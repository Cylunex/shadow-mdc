#!/usr/bin/env python3
"""Repair works seeded from JavDB detail pages before the panel-parser fix.

Until the JavDB detail parser read only ``.movie-panel-info`` (fix after f842b37),
works seeded via ``/v/<id>`` got the navbar dropdown labels as data:

- actors ``有碼 / 無碼 / 歐美`` (+ male actors), tags ``無碼 / 歐美 / FC2 / 動漫``
- studio ``無碼``, title equal to the 番号, ``primary_code`` NULL
- plot = the generic site description ``番號搜磁鏈，管理你的成人影片並分享你的想法``

For every affected work this script gets a clean JavDB record (re-fetch the detail page,
or with ``--from-snapshot`` reuse the stored javdb snapshot when it is already clean —
e.g. on the NAS after a sync imported corrected snapshots), then:

- if another work already holds the same 番号 (zero-padding tolerant: NIMA-086 = NIMA-86),
  the bad duplicate is deleted and the clean record is merged into the existing work
  (fill-only; ranking tags carried over, record code aligned to the survivor's code);
- otherwise the work's fields are rewritten from the clean record, keeping non-nav tags
  such as ``daily-chart-*``.

Orphaned actor rows created by the bad seeds (nav labels, dropped male actors) are removed
when they have no remaining work links, image or X handle. A JSON backup of every touched
row is written before ``--apply`` changes anything.

Example::

    PYTHONPATH=src uv run python scripts/repair_javdb_nav_label_works.py --dry-run
    PYTHONPATH=src uv run python scripts/repair_javdb_nav_label_works.py --apply

NAS (JavDB may be blocked there; run after the box→NAS sync so snapshots are clean)::

    SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\
    SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\
    PYTHONPATH=src .venv/bin/python scripts/repair_javdb_nav_label_works.py --from-snapshot --apply
"""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
from sqlalchemy import delete, select

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings
from shadow_mdc.db.models import (
    Actor,
    ExternalIdentity,
    MediaAsset,
    SourceSnapshot,
    Work,
    WorkActor,
    WorkCollection,
    WorkMagnet,
)
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.media.artwork import ArtworkStore
from shadow_mdc.providers.javdb import JavDBProvider, parse_javdb_detail

NAV_LABELS = frozenset({"有碼", "無碼", "歐美", "FC2", "動漫"})
NAV_TAG_LABELS = frozenset({"有碼", "無碼", "歐美", "動漫"})
SITE_PLOT_PREFIXES = ("番號搜磁鏈", "番号搜磁链")
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


def code_key(code: str | None) -> tuple[str, str] | None:
    if not code:
        return None
    match = re.match(r"^([A-Z0-9]*?[A-Z])-?0*(\d+)$", code.strip().upper())
    return (match.group(1), match.group(2)) if match else None


def is_bad(work: Work) -> bool:
    sources = dict(work.field_sources or {})
    if sources.get("title") != "javdb" and sources.get("actors") != "javdb":
        return False
    actors = set(work.actors or [])
    tags = set(work.tags or [])
    title = (work.title or "").strip()
    return bool(
        actors & NAV_LABELS
        or tags & NAV_TAG_LABELS
        or (work.studio in NAV_LABELS and sources.get("studio") == "javdb")
        or (work.plot or "").startswith(SITE_PLOT_PREFIXES)
        or work.primary_code is None
        or (work.primary_code and title.upper() == work.primary_code.upper())
        or (code_key(title) is not None and " " not in title)
    )


def record_is_clean(record: ProviderRecord) -> bool:
    return bool(
        record.code
        and not (set(record.actors) & NAV_LABELS)
        and not (set(record.tags) & NAV_TAG_LABELS)
        and record.title.strip().upper() != record.code.upper()
        and record.studio not in NAV_LABELS
    )


def _work_row(work: Work) -> dict[str, object]:
    return {
        column.name: (
            value.isoformat() if hasattr(value, "isoformat") else value
        )
        for column in Work.__table__.columns
        for value in [getattr(work, column.key)]
    }


async def _clean_record(
    repo: Repository,
    work: Work,
    *,
    javdb: JavDBProvider | None,
    client: httpx.AsyncClient | None,
    from_snapshot: bool,
    cache_dir: Path | None = None,
) -> ProviderRecord | None:
    session = repo._session  # noqa: SLF001 - maintenance script
    snapshot = session.scalar(
        select(SourceSnapshot).where(SourceSnapshot.work_id == work.id, SourceSnapshot.provider == "javdb")
    )
    identity = session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.work_id == work.id, ExternalIdentity.provider == "javdb"
        )
    )
    url = (identity.source_url if identity is not None else None) or None
    external_id = snapshot.external_id if snapshot is not None else (identity.value if identity else None)
    if snapshot is not None:
        payload = snapshot.payload if isinstance(snapshot.payload, dict) else json.loads(snapshot.payload)
        stored = ProviderRecord.model_validate(payload)
        if record_is_clean(stored):
            return stored
        url = url or stored.source_url
    if from_snapshot:
        return None
    if not url and external_id and javdb is not None:
        url = f"{javdb.base_url}/v/{external_id}"
    if not url or client is None:
        return None
    return parse_javdb_detail(await _fetch_detail_html(client, url, cache_dir=cache_dir), url)


_last_fetch = 0.0


async def _fetch_detail_html(client: httpx.AsyncClient, url: str, *, cache_dir: Path | None) -> str:
    """Polite JavDB fetch: ~3s spacing, backoff on 429, optional on-disk cache."""

    global _last_fetch
    cache_file = None
    if cache_dir is not None:
        cache_file = cache_dir / (url.rstrip("/").rsplit("/", 1)[-1] + ".html")
        if cache_file.is_file():
            return cache_file.read_text(encoding="utf-8")
    loop = asyncio.get_running_loop()
    for attempt in range(5):
        wait = 3.0 - (loop.time() - _last_fetch)
        if wait > 0:
            await asyncio.sleep(wait)
        _last_fetch = loop.time()
        response = await client.get(url, params={"locale": "zh"})
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After", "")
            await asyncio.sleep(float(retry_after) if retry_after.isdigit() else 20.0 * (attempt + 1))
            continue
        response.raise_for_status()
        if cache_file is not None:
            cache_file.parent.mkdir(parents=True, exist_ok=True)
            cache_file.write_text(response.text, encoding="utf-8")
        return response.text
    response.raise_for_status()
    return response.text


def _find_survivor(repo: Repository, bad: Work, record: ProviderRecord) -> Work | None:
    key = code_key(record.code)
    if key is None:
        return None
    session = repo._session  # noqa: SLF001
    for candidate in session.scalars(select(Work).where(Work.id != bad.id, Work.primary_code.is_not(None))):
        if code_key(candidate.primary_code) == key and not is_bad(candidate):
            return candidate
    return None


def _has_runtime_rows(repo: Repository, work_id: str) -> bool:
    session = repo._session  # noqa: SLF001
    return bool(
        session.scalar(select(MediaAsset.id).where(MediaAsset.work_id == work_id).limit(1))
        or session.scalar(select(WorkMagnet.id).where(WorkMagnet.work_id == work_id).limit(1))
    )


def _rewrite(repo: Repository, work: Work, record: ProviderRecord) -> None:
    kept_tags = [tag for tag in (work.tags or []) if tag not in NAV_LABELS]
    work.actors = []
    work.tags = []
    work.studio = None
    if (work.plot or "").startswith(SITE_PLOT_PREFIXES):
        work.plot = None
        work.original_plot = None
        sources = dict(work.field_sources or {})
        sources.pop("plot", None)
        work.field_sources = sources
    repo.merge_provider_into_work(work, record, overwrite=True)
    tags: list[str] = []
    for tag in [*record.tags, *kept_tags]:
        if tag not in tags:
            tags.append(tag)
    work.tags = tags
    sources = dict(work.field_sources or {})
    sources["tags"] = "manual"
    work.field_sources = sources
    work.updated_at = datetime.now(UTC).replace(tzinfo=None)


def _merge_into_survivor(repo: Repository, bad: Work, survivor: Work, record: ProviderRecord) -> None:
    carried = [tag for tag in (bad.tags or []) if tag not in NAV_LABELS and tag not in (survivor.tags or [])]
    session = repo._session  # noqa: SLF001
    # SQLite FK enforcement is off for the app engine, so ORM delete does not cascade:
    # drop dependents explicitly or the old javdb identity would block re-attaching it.
    for model in (WorkActor, ExternalIdentity, SourceSnapshot, WorkCollection):
        session.execute(delete(model).where(model.work_id == bad.id))
    session.delete(bad)
    session.flush()
    aligned = record.model_copy(update={"code": survivor.primary_code})
    repo.merge_provider_into_work(survivor, aligned, overwrite=False)
    if carried:
        survivor.tags = [*(survivor.tags or []), *carried]
    survivor.updated_at = datetime.now(UTC).replace(tzinfo=None)
    session.flush()


def _prune_orphan_actors(repo: Repository, names: set[str]) -> list[str]:
    session = repo._session  # noqa: SLF001
    removed: list[str] = []
    for name in sorted(names):
        actor = session.scalar(select(Actor).where(Actor.name == name))
        if actor is None or actor.image_url or actor.x_handle:
            continue
        linked = session.scalar(select(WorkActor.work_id).where(WorkActor.actor_id == actor.id).limit(1))
        if linked is not None:
            continue
        session.delete(actor)
        removed.append(name)
    session.flush()
    return removed


async def _run(arguments: argparse.Namespace) -> int:
    settings = Settings()
    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    if updates:
        settings = settings.model_copy(update=updates)
    database = Database(settings.database_url)
    database.initialize()

    client: httpx.AsyncClient | None = None
    javdb: JavDBProvider | None = None
    if not arguments.from_snapshot:
        client = httpx.AsyncClient(
            timeout=settings.request_timeout_seconds,
            follow_redirects=True,
            headers={"User-Agent": _BROWSER_UA, "Accept-Language": "zh-CN,zh;q=0.9,ja;q=0.6"},
            proxy=settings.proxy_url,
        )
        javdb = JavDBProvider(client, settings.javdb_base_url, settings.request_retries)

    report: list[dict[str, object]] = []
    backup: list[dict[str, object]] = []
    try:
        with database.session() as session:
            repo = Repository(session)
            bad_works = [work for work in session.scalars(select(Work)) if is_bad(work)]
            stale_actor_names: set[str] = set()
            for work in bad_works:
                entry: dict[str, object] = {"work_id": work.id, "before_title": work.title}
                try:
                    record = await _clean_record(
                        repo,
                        work,
                        javdb=javdb,
                        client=client,
                        from_snapshot=arguments.from_snapshot,
                        cache_dir=arguments.html_cache,
                    )
                except Exception as exc:  # noqa: BLE001 - report per work
                    entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
                    report.append(entry)
                    continue
                if record is None or not record_is_clean(record):
                    entry.update(ok=False, error="no clean javdb record (re-fetch needed)")
                    report.append(entry)
                    continue
                backup.append(
                    {
                        "work": _work_row(work),
                        "snapshots": [
                            {"provider": snap.provider, "external_id": snap.external_id, "payload": snap.payload}
                            for snap in session.scalars(
                                select(SourceSnapshot).where(SourceSnapshot.work_id == work.id)
                            )
                        ],
                    }
                )
                stale_actor_names |= set(work.actors or [])
                survivor = _find_survivor(repo, work, record)
                if survivor is not None and _has_runtime_rows(repo, work.id):
                    survivor = None  # never drop magnets/media; rewrite in place instead
                entry.update(
                    ok=True,
                    code=record.code,
                    title=record.title,
                    actors=list(record.actors),
                    tags=list(record.tags),
                    studio=record.studio,
                    release_date=record.release_date.isoformat() if record.release_date else None,
                    action="merge_into_existing" if survivor is not None else "rewrite",
                )
                if survivor is not None:
                    entry["survivor"] = {"work_id": survivor.id, "code": survivor.primary_code}
                if arguments.apply:
                    if survivor is not None:
                        _merge_into_survivor(repo, work, survivor, record)
                        target = survivor
                    else:
                        _rewrite(repo, work, record)
                        target = work
                    if client is not None and not arguments.no_posters and target.artwork:
                        has_local = any(
                            isinstance(item.get("local_path"), str) and Path(item["local_path"]).is_file()
                            for item in target.artwork
                        )
                        if not has_local:
                            art_result, local_paths = await ArtworkStore(
                                settings.data_dir / "artwork", client, max_bytes=settings.artwork_max_bytes
                            ).acquire(target)
                            if local_paths:
                                repo.update_artwork_local_paths(target, local_paths)
                            entry["artwork_downloaded"] = art_result.downloaded
                report.append(entry)
            if arguments.apply:
                session.flush()
                keep = {name for row in report if row.get("ok") for name in row.get("actors", [])}
                removed = _prune_orphan_actors(repo, stale_actor_names - keep)
                report.append({"orphan_actors_removed": removed})
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                backup_path = arguments.backup or (
                    settings.data_dir / "backups" / f"javdb-nav-label-repair-{stamp}.json"
                )
                backup_path.parent.mkdir(parents=True, exist_ok=True)
                backup_path.write_text(
                    json.dumps(backup, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8"
                )
                report.append({"backup": str(backup_path)})
            else:
                session.rollback()
    finally:
        if client is not None:
            await client.aclose()

    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0 if all(row.get("ok", True) for row in report) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--dry-run", action="store_true")
    mode.add_argument("--apply", action="store_true")
    parser.add_argument("--from-snapshot", action="store_true", help="offline: only use clean stored javdb snapshots")
    parser.add_argument("--no-posters", action="store_true")
    parser.add_argument("--html-cache", type=Path, default=None, help="reuse/save fetched detail pages here")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
