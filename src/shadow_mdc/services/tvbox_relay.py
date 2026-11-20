"""Restore the TVBox portion of a prepared Relay data export."""

import hashlib
import json
import re
import secrets
import tarfile
from collections import Counter
from datetime import UTC, datetime
from typing import BinaryIO

from pydantic import BaseModel, Field
from sqlalchemy import select

from ..db.models import TvBoxBinding, TvBoxGroup, TvBoxPack, TvBoxRelayRecord, TvBoxSite, utc_now
from ..db.repository import Database
from .tvbox_http import TvBoxError, http_url

FORMAT = "shadow-mdc-tvbox-relay/v1"
TABLES = frozenset(
    {
        "sources",
        "revisions",
        "catalogs",
        "candidates",
        "probes",
        "source_sets",
        "publications",
        "bindings",
        "preferences",
    }
)


class RelayRow(BaseModel):
    id: str = Field(min_length=1, max_length=200)
    data: dict[str, object]


class RelayTransfer(BaseModel):
    format: str
    tables: dict[str, list[RelayRow]]

    def rows(self, name: str) -> list[RelayRow]:
        return self.tables.get(name, [])


def prepare_transfer(transfer: RelayTransfer) -> RelayTransfer:
    if transfer.format not in ("shadow-relay-data/v1", FORMAT):
        raise TvBoxError("Relay 导出版本不受支持")
    sources = [row for row in transfer.rows("sources") if row.data.get("protocol") == "tvbox"]
    source_ids = {row.id for row in sources}
    revision_ids = {str(row.data.get(key)) for row in sources for key in ("activeRevision", "stagedRevision")}
    catalogs = {str(row.data.get("catalogId")) for row in sources}
    groups: list[RelayRow] = []
    for row in transfer.rows("source_sets"):
        members = row.data.get("members")
        if not isinstance(members, list):
            continue
        selected = [item for item in members if isinstance(item, dict) and item.get("sourceId") in source_ids]
        if selected:
            groups.append(row.model_copy(update={"data": row.data | {"members": selected}}))
    group_ids = {row.id for row in groups}

    tables = {
        "sources": sources,
        "revisions": [
            row
            for row in transfer.rows("revisions")
            if row.data.get("sourceId") in source_ids or row.id in revision_ids
        ],
        "source_sets": groups,
        "catalogs": [row for row in transfer.rows("catalogs") if row.id in catalogs],
        "candidates": [
            row
            for row in transfer.rows("candidates")
            if row.data.get("protocol") == "tvbox"
            or row.data.get("sourceId") in source_ids
            or row.data.get("catalogId") in catalogs
        ],
        "probes": [row for row in transfer.rows("probes") if row.data.get("sourceId") in source_ids],
        "bindings": [row for row in transfer.rows("bindings") if row.data.get("setId") in group_ids],
        "preferences": [row for row in transfer.rows("preferences") if row.data.get("setId") in group_ids],
        "publications": [],
    }
    for row in transfer.rows("publications"):
        if row.data.get("setId") not in group_ids:
            continue
        artifacts = row.data.get("artifacts")
        video_artifacts = (
            {key: value for key, value in artifacts.items() if key.startswith("tvbox/")}
            if isinstance(artifacts, dict)
            else {}
        )
        tables["publications"].append(
            row.model_copy(update={"data": row.data | {"artifacts": video_artifacts}})
        )
    return RelayTransfer(format=FORMAT, tables=tables)


def read_relay_upload(file: BinaryIO, filename: str) -> RelayTransfer:
    if filename.lower().endswith(".json"):
        raw = file.read(32 * 1024 * 1024 + 1)
        if len(raw) > 32 * 1024 * 1024:
            raise TvBoxError("Relay 清单超过大小限制")
        return prepare_transfer(RelayTransfer.model_validate_json(raw))
    return read_relay_archive(file)


def read_relay_archive(file: BinaryIO) -> RelayTransfer:
    scanned = 0
    try:
        with tarfile.open(fileobj=file, mode="r|gz") as archive:
            for index, member in enumerate(archive):
                scanned += member.size
                if scanned > 128 * 1024 * 1024 or index > 10000:
                    raise TvBoxError("Relay 归档超过读取限制")
                if member.name != "manifest.json":
                    continue
                if not member.isfile() or member.size > 32 * 1024 * 1024:
                    raise TvBoxError("Relay 清单大小或类型无效")
                handle = archive.extractfile(member)
                if handle is None:
                    break
                with handle:
                    return prepare_transfer(RelayTransfer.model_validate_json(handle.read()))
    except (tarfile.TarError, OSError) as exc:
        raise TvBoxError("无法读取 Relay 导出归档") from exc
    raise TvBoxError("归档缺少 Relay 清单")


def _legacy_key(kind: str, row_id: str) -> str:
    return hashlib.sha256((kind + ":" + row_id).encode()).hexdigest()[:32]


def _revision_sites(snapshot: RelayRow | None, allow_http: bool) -> list[dict[str, str | int]]:
    normalized = snapshot.data.get("normalized") if snapshot else None
    items = normalized.get("items") if isinstance(normalized, dict) else None
    sites: list[dict[str, str | int]] = []
    if isinstance(items, list):
        for row in items:
            if not isinstance(row, dict) or row.get("group") != "site":
                continue
            data = row.get("data")
            typ = str(data.get("type")) if isinstance(data, dict) else ""
            if typ not in ("0", "1"):
                continue
            try:
                endpoint = str(http_url(str(row.get("url", "")), allow_http=allow_http))
            except TvBoxError:
                continue
            sites.append(
                {
                    "key": str(row.get("id") or ""),
                    "name": str(row.get("name") or ""),
                    "type": int(typ),
                    "api": endpoint,
                }
            )
    return sites


def relay_config(transfer: RelayTransfer, allow_http: bool) -> str:
    revisions = {row.id: row for row in transfer.rows("revisions")}
    sites: dict[str, dict[str, str | int]] = {}
    for source in transfer.rows("sources"):
        if source.data.get("protocol") == "tvbox" and source.data.get("enabled") is True:
            for site in _revision_sites(revisions.get(str(source.data.get("activeRevision"))), allow_http):
                sites.setdefault(str(site["api"]), site)
    return json.dumps({"sites": list(sites.values())}, ensure_ascii=False)


def _date(value: object) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        date = datetime.fromisoformat(value)
        if date.year < 1970:
            return None
        return date.replace(tzinfo=UTC) if date.tzinfo is None else date.astimezone(UTC)
    except ValueError:
        return None


def _priority(member: dict[str, object]) -> tuple[bool, int]:
    value = member.get("priority")
    return (member.get("role") == "backup", -(value if isinstance(value, int) else 0))


def _ordered_group(
    group: RelayRow,
    members: list[object],
    source_sites: dict[str, list[str]],
    preferences: list[RelayRow],
    endpoints: dict[str, str],
) -> tuple[list[str], dict[str, str]]:
    normalized = sorted((member for member in members if isinstance(member, dict)), key=_priority)
    ids = list(
        dict.fromkeys(
            site for member in normalized for site in source_sites.get(str(member.get("sourceId")), [])
        )
    )
    aliases: dict[str, str] = {}
    pinned: list[str] = []
    for row in preferences:
        if row.data.get("setId") != group.id:
            continue
        compatible = source_sites.get(str(row.data.get("sourceId")), []) or ids
        exact = (
            endpoints.get(str(row.data.get("itemKey", "")).split("|")[1])
            if "|" in str(row.data.get("itemKey", ""))
            else None
        )
        site_id = exact if exact in compatible else compatible[0] if len(compatible) == 1 else None
        if not site_id or site_id not in ids:
            continue
        if row.data.get("hidden") is True or row.data.get("paused") is True:
            ids.remove(site_id)
            continue
        name = row.data.get("name")
        if isinstance(name, str) and name.strip():
            aliases[site_id] = name.strip()[:200]
        if row.data.get("pinned") is True:
            pinned.append(site_id)
    return list(dict.fromkeys([key for key in pinned if key in ids] + ids)), aliases


def archive_transfer(database: Database, transfer: RelayTransfer, allow_http: bool) -> dict[str, int]:
    revisions = {row.id: row for row in transfer.rows("revisions")}
    counts: Counter[str] = Counter()
    with database.session() as session:
        endpoints = {row.endpoint: row.id for row in session.scalars(select(TvBoxSite))}
        archived = set(session.execute(select(TvBoxRelayRecord.kind, TvBoxRelayRecord.legacy_id)).tuples())
        pending = []
        for kind in TABLES:
            for record in transfer.rows(kind):
                key = (kind, record.id)
                if key not in archived:
                    pending.append(TvBoxRelayRecord(kind=kind, legacy_id=record.id, data=record.data))
                    archived.add(key)
                counts[kind] += 1
        session.add_all(pending)
        source_sites: dict[str, list[str]] = {}
        for source in transfer.rows("sources"):
            selected = _revision_sites(revisions.get(str(source.data.get("activeRevision"))), allow_http)
            ids = [endpoints[str(site["api"])] for site in selected if str(site["api"]) in endpoints]
            if source.data.get("enabled") is not True:
                continue
            source_sites[source.id] = ids
            origin = source.data.get("url")
            if not isinstance(origin, str) or not origin:
                continue
            try:
                url = http_url(origin, allow_http=allow_http)
            except TvBoxError:
                continue
            if re.search(r"/p/[^/]+/(?:v/[^/]+/)?(?:tvbox/|sources/[^/]+/tvbox\.json)", url.path):
                counts["relay_links_archived"] += 1
                continue
            pack_id = hashlib.sha256(json.dumps((str(url),), ensure_ascii=False).encode()).hexdigest()[:32]
            if session.get(TvBoxPack, pack_id) is None:
                minutes = source.data.get("intervalMinutes")
                interval = max(5, min(43200, minutes)) if isinstance(minutes, int) and minutes > 0 else 1440
                if source.data.get("updatePolicy") in ("manual", "never"):
                    interval = 0
                session.add(
                    TvBoxPack(
                        id=pack_id,
                        url=str(url),
                        site_ids=list(dict.fromkeys(ids)),
                        allow_http=allow_http,
                        checked_at=utc_now(),
                        sync_minutes=interval,
                    )
                )
                counts["scheduled_origins"] += 1
        known_names = {row.name for row in session.scalars(select(TvBoxGroup))}
        group_mapping: dict[str, str] = {}
        for group in transfer.rows("source_sets"):
            members = group.data.get("members")
            if not isinstance(members, list):
                continue
            ids, aliases = _ordered_group(
                group, members, source_sites, transfer.rows("preferences"), endpoints
            )
            group_id = _legacy_key("group", group.id)
            group_mapping[group.id] = group_id
            if session.get(TvBoxGroup, group_id) is None:
                base = str(group.data.get("name") or "已迁移仓库")[:100]
                name = base if base not in known_names else f"{base} · {group_id[:8]}"
                known_names.add(name)
                session.add(TvBoxGroup(id=group_id, name=name, site_ids=ids, site_aliases=aliases))
        session.flush()
        for old in transfer.rows("bindings"):
            binding_group = group_mapping.get(str(old.data.get("setId")))
            formats = old.data.get("formats")
            if not binding_group or not isinstance(formats, list) or "tvbox/store.json" not in formats:
                continue
            binding_id = _legacy_key("binding", old.id)
            token_hash = str(old.data.get("tokenHash", ""))
            if session.get(TvBoxBinding, binding_id) is not None:
                continue
            needs_rotation = not re.fullmatch(r"[a-f0-9]{64}", token_hash)
            if not needs_rotation and session.scalar(
                select(TvBoxBinding.id).where(TvBoxBinding.token_hash == token_hash)
            ):
                needs_rotation = True
            if needs_rotation:
                counts["bindings_to_reissue"] += 1
                token_hash = secrets.token_hex(32)
            session.add(
                TvBoxBinding(
                    id=binding_id,
                    name=str(old.data.get("name") or "已迁移订阅")[:120],
                    group_id=group_id,
                    token_hash=token_hash,
                    needs_rotation=needs_rotation,
                    revoked=old.data.get("revoked") is True,
                    expires_at=_date(old.data.get("expiresAt")),
                )
            )
            counts["restored_bindings"] += 1
    return dict(counts)


class RelayRevisionView(BaseModel):
    id: str
    source: str
    state: str
    created_at: datetime | None


def relay_history(database: Database) -> list[RelayRevisionView]:
    with database.session() as session:
        sources = {
            row.legacy_id: str(row.data.get("name") or row.legacy_id)
            for row in session.scalars(select(TvBoxRelayRecord).where(TvBoxRelayRecord.kind == "sources"))
        }
        revisions = [
            RelayRevisionView(
                id=row.legacy_id,
                source=sources.get(str(row.data.get("sourceId")), "Relay"),
                state=str(row.data.get("status", "")),
                created_at=_date(row.data.get("createdAt")),
            )
            for row in session.scalars(select(TvBoxRelayRecord).where(TvBoxRelayRecord.kind == "revisions"))
        ]
        return sorted(
            revisions, key=lambda row: row.created_at or datetime(1970, 1, 1, tzinfo=UTC), reverse=True
        )[:200]


def historical_config(database: Database, revision_id: str, allow_http: bool) -> str:
    with database.session() as session:
        row = session.get(TvBoxRelayRecord, ("revisions", revision_id))
        if row is None:
            raise LookupError("Relay 修订不存在")
        sites = _revision_sites(RelayRow(id=row.legacy_id, data=row.data), allow_http)
        if not sites:
            raise TvBoxError("该历史修订没有可测试的 CMS 站点")
        return json.dumps({"sites": sites}, ensure_ascii=False)
