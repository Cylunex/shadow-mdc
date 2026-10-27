"""Reverse-import existing movie.nfo sidecars into Work records."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from math import isfinite
from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from ..db.models import MediaAsset, Work
from ..db.repository import Repository
from ..domain import MatchEvidence, ProviderRecord, ScoredCandidate
from ..enums import ContentFamily, MatchDecision, MediaCategory
from ..identity import extract_code
from ..remaster import parse_remaster_identity

_CODE_TAG = re.compile(r"(?i)^(?:num|id|code|jav)$")

SHADOW_LOCKS_TAG = "shadowmdc_locks"
# Emby <lockedfields> names → Work field names.
EMBY_LOCK_FIELDS: dict[str, str] = {
    "name": "title",
    "originaltitle": "original_title",
    "overview": "plot",
    "cast": "actors",
    "studios": "studio",
    "genres": "tags",
    "tags": "tags",
    "premieredate": "release_date",
    "productionyear": "release_date",
    "runtime": "runtime_seconds",
}
WORK_TO_EMBY_LOCK: dict[str, str] = {
    "title": "Name",
    "original_title": "OriginalTitle",
    "plot": "Overview",
    "actors": "Cast",
    "studio": "Studios",
    "tags": "Genres|Tags",
    "release_date": "PremiereDate|ProductionYear",
    "runtime_seconds": "Runtime",
}
NFO_LOCKABLE_FIELDS: tuple[str, ...] = (
    "title",
    "original_title",
    "plot",
    "actors",
    "studio",
    "series",
    "tags",
    "release_date",
    "runtime_seconds",
)


class ParsedNfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None
    original_title: str | None = None
    plot: str | None = None
    code: str | None = None
    studio: str | None = None
    series: str | None = None
    premiered: str | None = None
    runtime_seconds: int | None = None
    actors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    unique_ids: dict[str, str] = Field(default_factory=dict)
    # Work fields the user marked manual in the NFO (``<shadowmdc_locks>``,
    # Emby ``<lockedfields>``, or ``<lockdata>true`` = every field present).
    locked_fields: tuple[str, ...] = ()
    source_path: str


class NfoImportPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = "candidate"  # trust | candidate | ignore


class NfoImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scanned: int
    trusted: int
    candidates: int
    ignored: int
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class NfoImporter:
    repository: Repository
    policy: NfoImportPolicy = field(default_factory=NfoImportPolicy)

    def import_library_assets(self, assets: list[MediaAsset]) -> NfoImportResult:
        scanned = trusted = candidates = ignored = 0
        errors: list[str] = []
        for asset in assets:
            nfo_path = resolve_sidecar_nfo(Path(asset.path))
            if nfo_path is None:
                continue
            scanned += 1
            if self.policy.mode == "ignore":
                ignored += 1
                continue
            try:
                parsed = parse_movie_nfo(nfo_path)
            except (OSError, ElementTree.ParseError, ValueError) as exc:
                errors.append(f"{nfo_path}: {exc}")
                continue
            record = _to_provider_record(parsed)
            if self.policy.mode == "trust":
                work = self.repository.upsert_provider_record(record, overwrite=False)
                self.repository.attach_asset_to_work(asset, work)
                apply_nfo_locked_fields(self.repository, work, parsed)
                trusted += 1
            else:
                scored = ScoredCandidate(
                    record=record,
                    score=0.55,
                    decision=MatchDecision.REVIEW,
                    evidence=(MatchEvidence(kind="nfo", contribution=0.55, detail=str(nfo_path)),),
                )
                self.repository.save_candidates(asset, [scored])
                candidates += 1
        return NfoImportResult(
            scanned=scanned,
            trusted=trusted,
            candidates=candidates,
            ignored=ignored,
            errors=tuple(errors[:50]),
        )


def resolve_sidecar_nfo(media_path: Path) -> Path | None:
    """Prefer media-specific metadata before a shared directory-level movie.nfo."""

    directory = media_path.parent
    candidates = [
        media_path.with_suffix(".nfo"),
    ]
    stem = media_path.stem
    # CD1/CD2 share the base stem when possible
    for suffix in ("-CD1", "-CD2", "-cd1", "-cd2", "_1", "_2", "-A", "-B"):
        if stem.endswith(suffix):
            candidates.append(directory / f"{stem[: -len(suffix)]}.nfo")
            break
    candidates.append(directory / "movie.nfo")
    for path in candidates:
        if path.is_file():
            return path
    return None


def sibling_artwork_paths(media_path: Path) -> dict[str, Path]:
    directory = media_path.parent
    found: dict[str, Path] = {}
    for kind in ("poster", "fanart"):
        names = [
            f"{stem}.{extension}"
            for stem in (f"{media_path.stem}-{kind}", kind)
            for extension in ("jpg", "jpeg", "png", "webp", "gif")
        ]
        for name in names:
            path = directory / name
            if path.is_file():
                found[kind] = path
                break
    return found


def parse_movie_nfo(path: Path) -> ParsedNfo:
    tree = ElementTree.parse(path)
    root = tree.getroot()
    if root is None:
        raise ValueError("empty nfo")
    title = _text(root, "title")
    original = _text(root, "originaltitle")
    plot = _text(root, "plot")
    studio = _text(root, "studio")
    series = _text(root, "set/name") or _text(root, "set") or _text(root, "showtitle")
    premiered = _text(root, "premiered") or _text(root, "releasedate")
    code = _text(root, "num")
    legacy_id = _text(root, "id")
    legacy_node = root.find("id")
    legacy_kind = (legacy_node.get("type", "") if legacy_node is not None else "").strip().casefold()
    if not code and (not legacy_kind or _CODE_TAG.fullmatch(legacy_kind)):
        code = legacy_id
    actors = tuple(
        dict.fromkeys(
            name for actor in root.findall("actor") if (name := (actor.findtext("name") or "").strip())
        )
    )
    tags = tuple(
        dict.fromkeys(
            value
            for tag in list(root.findall("tag")) + list(root.findall("genre"))
            if (value := (tag.text or "").strip())
        )
    )
    unique_ids: dict[str, str] = {}
    for node in root.findall("uniqueid"):
        value = (node.text or "").strip()
        kind = (node.attrib.get("type") or "").strip().casefold() or "provider"
        if value:
            unique_ids[kind] = value
            if _CODE_TAG.match(kind) and not code:
                code = value
    if legacy_id and legacy_kind and not _CODE_TAG.fullmatch(legacy_kind):
        unique_ids.setdefault(legacy_kind, legacy_id)
    elif legacy_id and re.fullmatch(r"tt\d+", legacy_id, re.IGNORECASE):
        unique_ids.setdefault("imdb", legacy_id)
    if code:
        # extract_code returns (code, family); keep remaster tails (HD/AI/H) verbatim.
        extracted, _family = extract_code(code)
        literal = code.strip().upper()
        code = literal if parse_remaster_identity(literal) is not None else extracted
    if not title and not code:
        raise ValueError("nfo missing title and id")
    present = {
        "title": title,
        "original_title": original,
        "plot": plot,
        "actors": actors,
        "studio": studio,
        "series": series,
        "tags": tags,
        "release_date": premiered,
        "runtime_seconds": _runtime_seconds(_text(root, "runtime")),
    }
    locked = _parse_locked_fields(root, present)
    return ParsedNfo(
        title=title,
        original_title=original,
        plot=plot,
        code=code,
        studio=studio,
        series=series,
        premiered=premiered,
        runtime_seconds=_runtime_seconds(_text(root, "runtime")),
        actors=actors,
        tags=tags,
        unique_ids=unique_ids,
        locked_fields=locked,
        source_path=str(path.resolve()),
    )


def _runtime_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        minutes = float(value)
        return int(minutes * 60) if isfinite(minutes) and minutes >= 0 else None
    except (ValueError, OverflowError):
        return None


def _to_provider_record(parsed: ParsedNfo) -> ProviderRecord:
    release: date | None = None
    if parsed.premiered:
        try:
            release = date.fromisoformat(parsed.premiered[:10])
        except ValueError:
            release = None
    family = ContentFamily.JAV if parsed.code else ContentFamily.UNKNOWN
    category = MediaCategory.JAPAN if parsed.code else MediaCategory.OTHER
    if parsed.code:
        external_id = parsed.code
    elif "nfo-import" in parsed.unique_ids:
        external_id = parsed.unique_ids["nfo-import"]
    elif parsed.unique_ids:
        kind = next((name for name in ("imdb", "tmdb", "tvdb") if name in parsed.unique_ids), None)
        kind = kind or sorted(parsed.unique_ids)[0]
        external_id = f"{kind}:{parsed.unique_ids[kind]}"
    else:
        external_id = f"path:{sha256(parsed.source_path.encode('utf-8')).hexdigest()}"
    return ProviderRecord(
        provider="nfo-import",
        external_id=external_id,
        code=parsed.code,
        title=parsed.title or parsed.code or "Untitled",
        original_title=parsed.original_title,
        plot=parsed.plot,
        family=family,
        category=category,
        studio=parsed.studio,
        series=parsed.series,
        release_date=release,
        runtime_seconds=parsed.runtime_seconds,
        actors=parsed.actors,
        tags=parsed.tags,
        source_url=None,
    )


def _text(root: ElementTree.Element, tag: str) -> str | None:
    value = root.findtext(tag)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None


def _parse_locked_fields(root: ElementTree.Element, present: Mapping[str, object]) -> tuple[str, ...]:
    locked: list[str] = []
    own = _text(root, SHADOW_LOCKS_TAG)
    if own:
        locked.extend(item.strip() for item in re.split(r"[,|\s]+", own) if item.strip())
    emby = _text(root, "lockedfields")
    if emby:
        for item in emby.split("|"):
            mapped = EMBY_LOCK_FIELDS.get(item.strip().casefold())
            if mapped:
                locked.append(mapped)
    if (_text(root, "lockdata") or "").casefold() == "true":
        locked.extend(name for name, value in present.items() if value)
    return tuple(name for name in dict.fromkeys(locked) if name in NFO_LOCKABLE_FIELDS and present.get(name))


def apply_nfo_locked_fields(repository: Repository, work: Work, parsed: ParsedNfo) -> list[str]:
    """Adopt NFO values the user marked manual, then lock them against re-scrapes.

    In-app manual edits win: a field already locked in the DB is left as-is (the
    next export rewrites the NFO from the DB). Empty NFO values never apply, so
    the actor wipe fence in ``update_work_fields`` is never weakened.
    """

    db_locks = set(work.field_locks or [])
    todo = [name for name in parsed.locked_fields if name not in db_locks]
    if not todo:
        return []
    applied: list[str] = []
    edits: dict[str, object] = {}
    for name in todo:
        if name == "actors" and parsed.actors:
            if list(parsed.actors) != list(work.actors or []):
                edits["actors"] = list(parsed.actors)
            applied.append(name)
        elif name == "tags" and parsed.tags:
            if list(parsed.tags) != list(work.tags or []):
                edits["tags"] = list(parsed.tags)
            applied.append(name)
        elif name in {"title", "plot", "studio", "series"}:
            value = getattr(parsed, name)
            if value:
                current = work.title if name == "title" else getattr(work, name)
                if value != current and not (name == "title" and _same_display_title(value, work)):
                    edits[name] = value
                applied.append(name)
        elif name == "original_title" and parsed.original_title:
            work.original_title = parsed.original_title
            applied.append(name)
        elif name == "release_date" and parsed.premiered:
            try:
                work.release_date = date.fromisoformat(parsed.premiered[:10])
            except ValueError:
                continue
            applied.append(name)
        elif name == "runtime_seconds" and parsed.runtime_seconds is not None:
            work.runtime_seconds = parsed.runtime_seconds
            applied.append(name)
    if edits:
        repository.update_work_fields(work, lock_edited=False, **edits)  # type: ignore[arg-type]
    sources = dict(work.field_sources or {})
    for name in applied:
        if name not in edits:
            sources[name] = sources.get(name) or "nfo-manual"
        else:
            sources[name] = "nfo-manual"
    work.field_sources = sources
    repository.set_field_locks(work, sorted(db_locks | set(applied)))
    return applied


def _same_display_title(value: str, work: Work) -> bool:
    """NFO titles carry a ``CODE `` prefix added on export; treat those as unchanged."""

    code = work.primary_code
    if code and value.upper().startswith(code.upper()):
        return value[len(code) :].strip() == (work.title or "").strip()
    return False
