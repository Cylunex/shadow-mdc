"""Reverse-import existing movie.nfo sidecars into Work records."""

from __future__ import annotations

import ipaddress
import re
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from hashlib import sha256
from math import isfinite
from pathlib import Path, PurePosixPath
from stat import S_ISREG
from typing import TypedDict
from urllib.parse import unquote, urlsplit
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from ..db.models import MediaAsset, Work
from ..db.repository import Repository
from ..domain import MatchEvidence, NfoActorCredit, NfoDetails, ProviderRecord, ScoredCandidate
from ..enums import ContentFamily, MatchDecision, MediaCategory
from ..identity import extract_code
from ..normalize_code import is_format_equivalent
from ..remaster import parse_remaster_identity
from .parts import SidecarDirectory

_CODE_TAG = re.compile(r"(?i)^(?:num|id|code|jav)$")
NFO_COUNTRIES: dict[str, str] = {
    MediaCategory.JAPAN: "日本",
    MediaCategory.CHINA: "中国",
    MediaCategory.KOREA: "韩国",
    MediaCategory.EUROPE: "欧美",
}

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
    category: MediaCategory | None = None
    label: str | None = None
    series: str | None = None
    premiered: str | None = None
    runtime_seconds: int | None = None
    actors: tuple[str, ...] = ()
    details: NfoDetails = Field(default_factory=NfoDetails)
    directors: tuple[str, ...] = ()
    rating: float | None = None
    rating_max: float | None = None
    rating_count: int | None = None
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

    def import_library_assets(
        self, assets: list[MediaAsset], *, directories: Mapping[Path, SidecarDirectory] | None = None,
    ) -> NfoImportResult:
        layouts = dict(directories or {})
        scanned = trusted = candidates = ignored = 0
        errors: list[str] = []
        for asset in assets:
            path = Path(asset.path)
            if directories is None or path.parent not in directories:
                try:
                    if not S_ISREG(path.lstat().st_mode):
                        continue
                except FileNotFoundError:
                    continue
                except OSError as exc:
                    errors.append(f"{path}: {exc}")
                    continue
            if path.parent not in layouts:
                layouts[path.parent] = SidecarDirectory.inspect(path.parent)
            nfo_path = resolve_sidecar_nfo(path, layouts[path.parent])
            if nfo_path is None:
                continue
            scanned += 1
            if self.policy.mode == "ignore":
                ignored += 1
                continue
            try:
                parsed = parse_movie_nfo(nfo_path)
                expected = asset.hints.get("code")
                if not expected and asset.work_id is not None:
                    work = self.repository.get_work(asset.work_id)
                    expected = work.primary_code if work is not None else None
                if (
                    isinstance(expected, str)
                    and parsed.code
                    and not is_format_equivalent(expected, parsed.code)
                ):
                    raise ValueError(f"NFO code conflict: expected={expected}; received={parsed.code}")
            except (OSError, ValueError) as exc:
                errors.append(f"{nfo_path}: {exc}")
                continue
            record = nfo_provider_record(parsed)
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


def resolve_sidecar_nfo(media_path: Path, layout: SidecarDirectory | None = None) -> Path | None:
    """Read media-specific metadata, then fall back only within a single-work directory."""

    layout = layout or SidecarDirectory.inspect(media_path.parent)
    stem = media_path.stem
    candidates: list[Path] = []
    if layout.owns_stem(stem):
        candidates.append(media_path.with_suffix(".nfo"))
    if layout.shared:
        if base := layout.part_stem(stem):
            candidates.append(media_path.with_name(f"{base}.nfo"))
        candidates.append(media_path.parent / "movie.nfo")
    return next((path for path in candidates if path.is_file()), None)


def sibling_artwork_paths(media_path: Path, layout: SidecarDirectory | None = None) -> dict[str, Path]:
    layout = layout or SidecarDirectory.inspect(media_path.parent)
    found: dict[str, Path] = {}
    for kind in ("poster", "fanart"):
        stems = [f"{media_path.stem}-{kind}"] if layout.owns_stem(media_path.stem) else []
        if layout.shared:
            stems.append(kind)
        paths = (media_path.with_name(f"{stem}.{ext}") for stem in stems
                 for ext in ("jpg", "jpeg", "png", "webp", "gif"))
        if (path := next((path for path in paths if path.is_file()), None)) is not None:
            found[kind] = path
    return found


def read_movie_element(path: Path, *, allow_wrapper: bool = False) -> ElementTree.Element:
    try:
        root = ElementTree.parse(path).getroot()
    except (ElementTree.ParseError, LookupError) as exc:
        raise ValueError(f"invalid NFO XML: {exc}") from exc
    if root is not None:
        if root.tag.lower() == "movie":
            return root
        if allow_wrapper and (movie := root.find("movie")) is not None:
            return movie
    raise ValueError("expected <movie> NFO root")


def parse_movie_nfo(path: Path, *, allow_wrapper: bool = False) -> ParsedNfo:
    root = read_movie_element(path, allow_wrapper=allow_wrapper)
    title = _text(root, "title")
    original = _text(root, "originaltitle")
    plot = _text(root, "plot")
    studio = _text(root, "studio")
    country = (_text(root, "country") or "").casefold()
    category = next((
        value for value in MediaCategory
        if country in (value.casefold(), NFO_COUNTRIES.get(value, value).casefold())
    ), None)
    series_names = [
        name for node in root.findall("set")
        if (name := _text(node, "name") or (node.text or "").strip()) and len(name) <= 300
    ]
    if not series_names and (show := _text(root, "showtitle")) and len(show) <= 300:
        series_names.append(show)
    series_names.extend(
        name for node in root.findall("shadowmdc_series/name")
        if (name := (node.text or "").strip()) and len(name) <= 300
    )
    series = tuple(dict.fromkeys(series_names))[:100]
    premiered = _text(root, "premiered") or _text(root, "releasedate")
    code = _text(root, "num")
    legacy_id = _text(root, "id")
    legacy_node = root.find("id")
    legacy_kind = (legacy_node.get("type", "") if legacy_node is not None else "").strip().casefold()
    if not code and (not legacy_kind or _CODE_TAG.fullmatch(legacy_kind)):
        code = legacy_id
    cast = tuple(credit for node in root.findall("actor")[:500] if (credit := _nfo_actor(node)) is not None)
    actors = tuple(dict.fromkeys(credit.name for credit in cast))
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
        "series": series[0] if series else None,
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
        category=category,
        label=_text(root, "label"),
        series=series[0] if series else None,
        premiered=premiered,
        runtime_seconds=_runtime_seconds(_text(root, "runtime")),
        actors=actors,
        details=NfoDetails(series=series, cast=cast),
        directors=tuple(dict.fromkeys(value for node in root.findall("director") if (value := (node.text or "").strip()))),
        tags=tags,
        unique_ids=unique_ids,
        locked_fields=locked,
        source_path=str(path.resolve()),
        **_rating_fields(root),
    )


def safe_actor_thumb(value: str | None) -> str | None:
    """Keep a public URL or portable relative artwork reference without reading it."""
    if not value or len(value) > 2048 or any(ord(char) < 32 for char in value):
        return None
    try:
        parsed = urlsplit(value)
        if parsed.scheme:
            host = parsed.hostname
            if parsed.scheme not in {"http", "https"} or not host or parsed.username or parsed.password:
                return None
            if host.casefold().rstrip(".") == "localhost" or host.casefold().rstrip(".").endswith(".local"):
                return None
            try:
                if not ipaddress.ip_address(host).is_global:
                    return None
            except ValueError:
                pass
            return value
    except ValueError:
        return None
    portable = value.replace("\\", "/")
    path = PurePosixPath(unquote(portable))
    if path.is_absolute() or ".." in path.parts or not path.name or parsed.netloc or parsed.query or parsed.fragment:
        return None
    return portable


def _nfo_actor(node: ElementTree.Element) -> NfoActorCredit | None:
    name = _text(node, "name")
    if not name or len(name) > 300:
        return None
    raw_order = _text(node, "order") or ""
    order = int(raw_order) if raw_order.isascii() and raw_order.isdecimal() and len(raw_order) < 6 else None
    return NfoActorCredit(
        name=name, role=(_text(node, "role") or "")[:500] or None,
        order=order if order is not None and order <= 10000 else None,
        thumb=safe_actor_thumb(_text(node, "thumb")),
    )


class _NfoRating(TypedDict, total=False):
    rating: float
    rating_max: float
    rating_count: int


def _rating_fields(root: ElementTree.Element) -> _NfoRating:
    ratings = root.findall("ratings/rating")
    for node in sorted(ratings, key=lambda rating: rating.get("default", "").casefold() != "true"):
        try:
            value, maximum = float(_text(node, "value") or ""), float(node.get("max", "10"))
        except ValueError:
            continue
        if not (isfinite(value) and isfinite(maximum) and 0 <= value <= maximum and maximum > 0):
            continue
        result: _NfoRating = {"rating": value, "rating_max": maximum}
        votes = _text(node, "votes")
        if votes and votes.isascii() and votes.isdecimal() and len(votes) <= 10:
            result["rating_count"] = int(votes)
        return result
    return {}


def _runtime_seconds(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        minutes = float(value)
        return int(minutes * 60) if isfinite(minutes) and minutes >= 0 else None
    except (ValueError, OverflowError):
        return None


def nfo_provider_record(parsed: ParsedNfo) -> ProviderRecord:
    release: date | None = None
    if parsed.premiered:
        try:
            release = date.fromisoformat(parsed.premiered[:10])
        except ValueError:
            release = None
    family = ContentFamily.JAV if parsed.code else ContentFamily.UNKNOWN
    category = parsed.category or (MediaCategory.JAPAN if parsed.code else MediaCategory.OTHER)
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
        label=parsed.label,
        series=parsed.series,
        release_date=release,
        runtime_seconds=parsed.runtime_seconds,
        actors=parsed.actors,
        directors=parsed.directors,
        rating=parsed.rating,
        rating_max=parsed.rating_max,
        rating_count=parsed.rating_count,
        tags=parsed.tags,
        nfo_details=parsed.details,
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
