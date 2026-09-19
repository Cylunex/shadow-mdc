"""Seed JavDB yearly TOP250 (2008+) from jinjier SQLite into javranking cache + catalog."""

from __future__ import annotations

import hashlib
import re
import sqlite3
import time
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..domain import Artwork, ProviderRecord
from ..enums import ContentFamily, MediaCategory
from ..normalize_code import normalize_code, to_comparison_key
from .daily_chart_seed import merge_tags
from .javranking_client import (
    DEFAULT_BASE_URL,
    DEFAULT_LOCALE,
    CuratedList,
    CuratedVideoEntry,
    JavRankingIndexCache,
    sha256_hex16,
)

PROVIDER = "javdb-yearly-top250"
YEARLY_NOTE_RE = re.compile(r"^JavDB (?P<year>20\d{2}) TOP250$")
YEARLY_SLUG_RE = re.compile(r"^javdb-top250-(?P<year>20\d{2})$")
DEFAULT_YEAR_START = 2008
DEFAULT_YEAR_END = 2024


class YearlyTop250Entry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    year: int = Field(ge=2000, le=2100)
    position: int = Field(ge=1)
    code: str
    title: str
    release_date: str | None = None
    cover_url: str | None = None
    raw_name: str = ""


class YearlyTop250SeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    years: tuple[int, ...] = ()
    lists_written: tuple[str, ...] = ()
    entry_counts: dict[str, int] = Field(default_factory=dict)
    created: int = 0
    updated: int = 0
    skipped: int = 0
    dry_run: bool = False


@dataclass(frozen=True)
class _ParsedName:
    code: str
    title: str


def yearly_slug(year: int) -> str:
    return f"javdb-top250-{year}"


def yearly_title(year: int) -> str:
    return f"JavDB {year} TOP250"


def parse_code_from_jinjier_name(name: str) -> _ParsedName:
    """Extract code + remainder title from a jinjier ``ranks.name`` value.

    Codes are the first whitespace-delimited token. Handles standard JAV codes,
    uncensored tokens (``n0299``, ``091208_424``, ``MKBD-S03``), then normalizes
    via :func:`normalize_code`.
    """

    text = (name or "").strip()
    if not text:
        return _ParsedName(code="", title="")
    parts = text.split(None, 1)
    raw_code = parts[0].strip()
    remainder = parts[1].strip() if len(parts) > 1 else ""
    # Drop leading 無碼 / 有码 markers that sometimes stick to the title.
    if remainder:
        remainder = re.sub(r"^(無碼|无码|有碼|有码)\s+", "", remainder).strip()
    code = normalize_code(raw_code) or raw_code.upper()
    title = remainder or code
    return _ParsedName(code=code, title=title)


def note_year(note: str) -> int | None:
    match = YEARLY_NOTE_RE.fullmatch((note or "").strip())
    if match is None:
        return None
    return int(match.group("year"))


def slug_year(slug: str) -> int | None:
    match = YEARLY_SLUG_RE.fullmatch((slug or "").strip())
    if match is None:
        return None
    return int(match.group("year"))


def list_available_years(sqlite_path: Path) -> list[int]:
    if not sqlite_path.is_file():
        return []
    with sqlite3.connect(str(sqlite_path)) as conn:
        rows = conn.execute(
            "SELECT DISTINCT note FROM ranks WHERE note LIKE 'JavDB % TOP250'"
        ).fetchall()
    years: list[int] = []
    for (note,) in rows:
        year = note_year(str(note or ""))
        if year is not None:
            years.append(year)
    return sorted(set(years))


def load_yearly_top250_from_sqlite(
    sqlite_path: Path,
    *,
    years: Sequence[int] | None = None,
    limit_per_year: int | None = None,
) -> list[YearlyTop250Entry]:
    """Load yearly TOP250 rows from jinjier SQLite.

    Dedupes by comparison-key within each year, keeping the best (lowest) rank.
    """

    if not sqlite_path.is_file():
        raise FileNotFoundError(f"jinjier sqlite not found: {sqlite_path}")

    wanted = set(int(y) for y in years) if years is not None else None
    with sqlite3.connect(str(sqlite_path)) as conn:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT number, name, date, icon_url, note FROM ranks "
            "WHERE note LIKE 'JavDB % TOP250' "
            "ORDER BY note, CAST(number AS INT)"
        ).fetchall()

    by_year: dict[int, dict[str, YearlyTop250Entry]] = {}
    for row in rows:
        year = note_year(str(row["note"] or ""))
        if year is None:
            continue
        if wanted is not None and year not in wanted:
            continue
        try:
            position = int(str(row["number"]).strip())
        except (TypeError, ValueError):
            continue
        if position < 1:
            continue
        parsed = parse_code_from_jinjier_name(str(row["name"] or ""))
        if not parsed.code:
            continue
        key = to_comparison_key(parsed.code)
        if not key:
            continue
        release = str(row["date"] or "").strip() or None
        if release and len(release) >= 10:
            release = release[:10]
        cover = str(row["icon_url"] or "").strip() or None
        entry = YearlyTop250Entry(
            year=year,
            position=position,
            code=parsed.code,
            title=parsed.title,
            release_date=release,
            cover_url=cover,
            raw_name=str(row["name"] or ""),
        )
        bucket = by_year.setdefault(year, {})
        existing = bucket.get(key)
        if existing is None or entry.position < existing.position:
            bucket[key] = entry

    results: list[YearlyTop250Entry] = []
    for year in sorted(by_year):
        ordered = sorted(by_year[year].values(), key=lambda item: item.position)
        if limit_per_year is not None:
            ordered = ordered[: max(0, int(limit_per_year))]
        # Re-number densely after dedupe/limit so UI ranks stay 1..N.
        for index, entry in enumerate(ordered, start=1):
            if entry.position != index:
                entry = entry.model_copy(update={"position": index})
            results.append(entry)
    return results


def entries_by_year(entries: Sequence[YearlyTop250Entry]) -> dict[int, list[YearlyTop250Entry]]:
    grouped: dict[int, list[YearlyTop250Entry]] = {}
    for entry in entries:
        grouped.setdefault(entry.year, []).append(entry)
    for year in grouped:
        grouped[year].sort(key=lambda item: item.position)
    return grouped


def curated_list_from_entries(
    year: int,
    entries: Sequence[YearlyTop250Entry],
    *,
    base_url: str = DEFAULT_BASE_URL,
    locale: str = DEFAULT_LOCALE,
    fetched_at: float | None = None,
) -> CuratedList:
    slug = yearly_slug(year)
    title = yearly_title(year)
    videos = tuple(
        CuratedVideoEntry(
            position=entry.position,
            code=entry.code,
            title=entry.title,
            video_id=None,
            url=None,
            cover_url=entry.cover_url,
        )
        for entry in sorted(entries, key=lambda item: item.position)
    )
    payload = "".join(f"{v.position}:{v.code}:{v.title}\n" for v in videos)
    return CuratedList(
        slug=slug,
        title=title,
        kind="videos",
        locale=locale,
        base_url=base_url.rstrip("/"),
        revision=sha256_hex16(payload),
        fetched_at=fetched_at if fetched_at is not None else time.time(),
        source_format="jinjier-sqlite",
        canonical_url=None,
        videos=videos,
        actors=(),
    )


def write_yearly_list_files(
    cache_dir: Path,
    entries: Sequence[YearlyTop250Entry],
    *,
    base_url: str = DEFAULT_BASE_URL,
    locale: str = DEFAULT_LOCALE,
    force: bool = False,
) -> list[str]:
    """Write canonical ``yearly/javdb-top250-{year}.json`` plus curated list cache.

    Historical yearly JSON under ``yearly/`` is frozen once written (unless
    ``force``). Curated ``list-*.json`` mirrors stay for seed/catalog tooling.
    Prefer exporting on the **box** then rsync ``yearly/`` to NAS — do not
    re-fetch historical years on NAS.
    """

    from .javdb_yearly_top250 import (
        YearlyTop250Item,
        last_completed_year,
        listing_from_items,
        read_yearly_list,
        write_yearly_list,
        yearly_dir,
    )

    # cache_dir is data/javranking; data_dir is its parent
    data_dir = cache_dir.parent if cache_dir.name == "javranking" else cache_dir
    cache = JavRankingIndexCache(cache_dir, base_url=base_url, locale=locale)
    written: list[str] = []
    completed = last_completed_year()
    for year, year_entries in entries_by_year(entries).items():
        items = [
            YearlyTop250Item(
                rank=entry.position,
                code=entry.code,
                title=entry.title,
                date=entry.release_date,
                icon_url=entry.cover_url,
            )
            for entry in year_entries
        ]
        frozen = year <= completed
        listing = listing_from_items(
            year, items, source="jinjier", frozen=frozen
        )
        existing = read_yearly_list(data_dir, year)
        if existing is not None and existing.frozen and not force:
            # Immutable historical file — keep on disk, still refresh curated mirror.
            pass
        else:
            write_yearly_list(data_dir, listing, force=force or not frozen)
        curated = curated_list_from_entries(
            year, year_entries, base_url=base_url, locale=locale
        )
        cache.write_curated_list(curated)
        written.append(curated.slug)
    yearly_dir(data_dir).mkdir(parents=True, exist_ok=True)
    return written


def discover_yearly_top250_on_disk(
    cache_dir: Path,
) -> list[tuple[str, int, str, int]]:
    """Return ``(slug, year, title, item_count)`` for on-disk yearly TOP250 lists.

    Prefers canonical ``yearly/javdb-top250-*.json``; falls back to curated
    ``list-javdb-top250-*.json`` mirrors.
    """

    from .javdb_yearly_top250 import list_local_yearly_years, read_yearly_list

    data_dir = cache_dir.parent if cache_dir.name == "javranking" else cache_dir
    found_map: dict[str, tuple[str, int, str, int]] = {}
    for year in list_local_yearly_years(data_dir):
        listing = read_yearly_list(data_dir, year)
        if listing is None:
            continue
        found_map[listing.slug] = (
            listing.slug,
            listing.year,
            listing.title,
            len(listing.items),
        )

    if cache_dir.is_dir():
        cache = JavRankingIndexCache(cache_dir)
        for path in sorted(cache_dir.glob("list-javdb-top250-*.json")):
            slug = path.name.removeprefix("list-").removesuffix(".json")
            year = slug_year(slug)
            if year is None or slug in found_map:
                continue
            curated = cache.read_curated_list(slug)
            if curated is None or curated.kind != "videos":
                continue
            found_map[slug] = (
                slug,
                year,
                curated.title or yearly_title(year),
                len(curated.videos),
            )
    found = sorted(found_map.values(), key=lambda item: (-item[1], item[0]))
    return found


def merge_top250_year_sections(
    index_sections: Sequence[tuple[str, int | None, str]],
    disk_sections: Sequence[tuple[str, int, str, int]],
    *,
    year_start: int = DEFAULT_YEAR_START,
    year_end: int | None = None,
) -> list[tuple[str, int | None, str, int | None]]:
    """Merge disk-first yearly TOP250 sections with optional index/current-year rows.

    Historical years must come from local files (disk counts). Index-only rows
    are kept for the current calendar year when disk is missing. Always emit
    chips for ``year_start..year_end`` so the UI can show 2008→now.
    """

    from datetime import date as date_cls

    end = year_end if year_end is not None else date_cls.today().year
    by_slug: dict[str, tuple[str, int | None, str, int | None]] = {}
    # Disk wins for historical / already-exported years.
    for slug, year, title, count in disk_sections:
        by_slug[slug] = (slug, year, title, count)
    for slug, year, name in index_sections:
        if slug in by_slug:
            continue
        by_slug[slug] = (slug, year, name, None)
    for year in range(year_start, end + 1):
        slug = yearly_slug(year)
        if slug not in by_slug:
            by_slug[slug] = (slug, year, yearly_title(year), 0)
    return sorted(by_slug.values(), key=lambda item: (-(item[1] or 0), item[0]))


def _parse_release(value: str | None, year: int) -> date | None:
    if value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    return date(year, 1, 1)


def _to_provider_record(entry: YearlyTop250Entry) -> ProviderRecord:
    year_tag = f"javdb-top250-{entry.year}"
    rank_tag = f"javdb-top250-{entry.year}-rank-{entry.position}"
    tags = ("jav", "javdb-top250", year_tag, rank_tag, PROVIDER)
    artwork = ()
    if entry.cover_url and entry.cover_url.startswith(("http://", "https://")):
        artwork = (Artwork.model_validate({"url": entry.cover_url, "kind": "poster"}),)
    external_id = f"{PROVIDER}:{entry.year}:{entry.code}"
    return ProviderRecord(
        provider=PROVIDER,
        external_id=external_id,
        source_url=None,
        code=entry.code,
        title=entry.title,
        original_title=entry.raw_name or entry.title,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        release_date=_parse_release(entry.release_date, entry.year),
        tags=tags,
        artwork=artwork,
        language="ja",
    )


def seed_catalog_from_entries(
    repo: Repository,
    entries: Sequence[YearlyTop250Entry],
    *,
    dry_run: bool = False,
    limit: int | None = None,
) -> YearlyTop250SeedResult:
    """Upsert catalog-only works + merge ranking tags. Idempotent."""

    created = 0
    updated = 0
    skipped = 0
    processed = 0
    # Prefer best rank per code across years when limiting, but still tag all years
    # when not limited. Process year-by-year in chrono order for stability.
    for entry in entries:
        if limit is not None and processed >= limit:
            skipped += 1
            continue
        processed += 1
        record = _to_provider_record(entry)
        if dry_run:
            existing = repo.find_work_by_code(entry.code)
            if existing is None:
                created += 1
            else:
                updated += 1
            continue
        existing = repo.find_work_by_code(entry.code)
        work = repo.upsert_provider_record(record, overwrite=False)
        year_tag = f"javdb-top250-{entry.year}"
        rank_tag = f"javdb-top250-{entry.year}-rank-{entry.position}"
        tags = merge_tags(work.tags or [], ("jav", "javdb-top250", year_tag, rank_tag, PROVIDER))
        repo.update_work_fields(work, tags=tags, lock_edited=False)
        if existing is None:
            created += 1
        else:
            updated += 1

    counts = {
        yearly_slug(year): len(items) for year, items in entries_by_year(entries).items()
    }
    return YearlyTop250SeedResult(
        years=tuple(sorted(entries_by_year(entries))),
        lists_written=(),
        entry_counts=counts,
        created=created,
        updated=updated,
        skipped=skipped,
        dry_run=dry_run,
    )


def seed_javdb_yearly_top250(
    *,
    sqlite_path: Path,
    data_dir: Path,
    repo: Repository | None = None,
    years: Sequence[int] | None = None,
    limit_per_year: int | None = None,
    catalog_limit: int | None = None,
    dry_run: bool = False,
    write_lists: bool = True,
    seed_catalog: bool = True,
    base_url: str = DEFAULT_BASE_URL,
    locale: str = DEFAULT_LOCALE,
) -> YearlyTop250SeedResult:
    """Full pipeline: load sqlite → write yearly list JSON → optional catalog upsert."""

    resolved_years = list(years) if years is not None else None
    if resolved_years is None:
        available = list_available_years(sqlite_path)
        resolved_years = [
            year
            for year in available
            if DEFAULT_YEAR_START <= year <= DEFAULT_YEAR_END or year >= 2025
        ]
        if not resolved_years:
            resolved_years = available

    entries = load_yearly_top250_from_sqlite(
        sqlite_path,
        years=resolved_years,
        limit_per_year=limit_per_year,
    )
    lists_written: list[str] = []
    if write_lists and not dry_run:
        lists_written = write_yearly_list_files(
            data_dir / "javranking",
            entries,
            base_url=base_url,
            locale=locale,
        )
    elif write_lists and dry_run:
        lists_written = [yearly_slug(year) for year in entries_by_year(entries)]

    catalog_result = YearlyTop250SeedResult(
        years=tuple(sorted(entries_by_year(entries))),
        lists_written=tuple(lists_written),
        entry_counts={
            yearly_slug(year): len(items) for year, items in entries_by_year(entries).items()
        },
        dry_run=dry_run,
    )
    if seed_catalog and repo is not None:
        catalog_result = seed_catalog_from_entries(
            repo,
            entries,
            dry_run=dry_run,
            limit=catalog_limit,
        )
        catalog_result = catalog_result.model_copy(
            update={
                "lists_written": tuple(lists_written),
                "entry_counts": {
                    yearly_slug(year): len(items)
                    for year, items in entries_by_year(entries).items()
                },
                "years": tuple(sorted(entries_by_year(entries))),
            }
        )
    return catalog_result


def content_revision(entries: Sequence[YearlyTop250Entry]) -> str:
    digest = hashlib.sha256()
    for entry in entries:
        digest.update(
            f"{entry.year}:{entry.position}:{entry.code}:{entry.title}\n".encode("utf-8")
        )
    return digest.hexdigest()[:16]
