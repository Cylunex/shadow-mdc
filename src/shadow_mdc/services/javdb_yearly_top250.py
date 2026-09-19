"""Local-first JavDB yearly TOP250 lists under ``data/javranking/yearly/``.

Historical years are immutable once written (export from jinjier SQLite on the
box, then rsync to NAS). Only the current calendar year may optionally refresh
from the remote JavRanking index through a proxy.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..normalize_code import normalize_code, to_comparison_key
from .javranking_client import SearchVideo

logger = logging.getLogger(__name__)

YEARLY_DIR_NAME = "yearly"
SLUG_PREFIX = "javdb-top250"
NOTE_RE = re.compile(r"^JavDB\s+(\d{4})\s+TOP250$")
FIRST_YEAR = 2008


class YearlyTop250Item(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    rank: int = Field(ge=1)
    code: str
    title: str
    date: str | None = None
    icon_url: str | None = None


class YearlyTop250List(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    slug: str
    year: int
    title: str
    source: str  # jinjier | javranking-index | remote
    frozen: bool = True
    exported_at: str
    revision: str
    items: tuple[YearlyTop250Item, ...] = ()

    @field_validator("items", mode="before")
    @classmethod
    def _tupleize(cls, value: object) -> object:
        if value is None:
            return ()
        return value


def yearly_dir(data_dir: Path) -> Path:
    return data_dir / "javranking" / YEARLY_DIR_NAME


def yearly_slug(year: int) -> str:
    return f"{SLUG_PREFIX}-{year}"


def yearly_path(data_dir: Path, year: int) -> Path:
    return yearly_dir(data_dir) / f"{yearly_slug(year)}.json"


def last_completed_year(*, today: date | None = None) -> int:
    current = (today or date.today()).year
    return current - 1


def current_calendar_year(*, today: date | None = None) -> int:
    return (today or date.today()).year


def parse_code_and_title(name: str) -> tuple[str | None, str]:
    """Parse 番号 from jinjier ``name`` (token before first whitespace)."""

    text = (name or "").strip()
    if not text:
        return None, ""
    parts = text.split(None, 1)
    raw_code = parts[0].strip()
    rest = parts[1].strip() if len(parts) > 1 else ""
    # Strip leading 無碼 / 有码 markers that sometimes glue oddly
    code = normalize_code(raw_code) or raw_code.upper()
    if not code or len(code) < 3:
        return None, text
    title = rest or text
    return code, title


def revision_for_items(items: Sequence[YearlyTop250Item]) -> str:
    payload = json.dumps(
        [item.model_dump(mode="json") for item in items],
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def dedupe_items(rows: Sequence[YearlyTop250Item]) -> list[YearlyTop250Item]:
    """Keep one row per rank number (lowest wins). Preserve duplicate codes.

    Older jinjier years sometimes list the same code at multiple ranks; the
    yearly TOP250 UI should still show 250 slots. Catalog seeding may dedupe
    by comparison key separately.
    """

    by_rank: dict[int, YearlyTop250Item] = {}
    for item in sorted(rows, key=lambda row: row.rank):
        if item.rank not in by_rank:
            by_rank[item.rank] = item
    return [by_rank[rank] for rank in sorted(by_rank)]


def read_yearly_list(data_dir: Path, year: int) -> YearlyTop250List | None:
    path = yearly_path(data_dir, year)
    if not path.is_file():
        return None
    try:
        return YearlyTop250List.model_validate_json(path.read_text(encoding="utf-8"))
    except Exception as exc:
        logger.warning("failed to read yearly TOP250 %s: %s", path, exc)
        return None


def write_yearly_list(
    data_dir: Path,
    listing: YearlyTop250List,
    *,
    force: bool = False,
) -> Path:
    path = yearly_path(data_dir, listing.year)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_file() and not force:
        existing = read_yearly_list(data_dir, listing.year)
        if existing is not None and existing.frozen and listing.year <= last_completed_year():
            raise FileExistsError(
                f"refusing to overwrite frozen yearly list {path} (pass force=True)"
            )
    path.write_text(
        listing.model_dump_json(indent=2),
        encoding="utf-8",
    )
    return path


def list_local_yearly_years(data_dir: Path) -> list[int]:
    root = yearly_dir(data_dir)
    if not root.is_dir():
        return []
    years: list[int] = []
    for path in root.glob(f"{SLUG_PREFIX}-*.json"):
        stem = path.stem  # javdb-top250-2024
        suffix = stem.removeprefix(f"{SLUG_PREFIX}-")
        if suffix.isdigit():
            years.append(int(suffix))
    return sorted(years)


def expected_year_range(*, today: date | None = None) -> range:
    end = current_calendar_year(today=today)
    return range(FIRST_YEAR, end + 1)


def load_from_jinjier(
    sqlite_path: Path,
    *,
    years: Sequence[int] | None = None,
) -> dict[int, list[YearlyTop250Item]]:
    """Read ``ranks`` rows for ``JavDB YYYY TOP250`` notes."""

    if not sqlite_path.is_file():
        raise FileNotFoundError(f"jinjier sqlite not found: {sqlite_path}")
    conn = sqlite3.connect(str(sqlite_path))
    try:
        cur = conn.cursor()
        if years is None:
            cur.execute(
                "SELECT DISTINCT note FROM ranks WHERE note LIKE 'JavDB % TOP250' ORDER BY note"
            )
            notes = [row[0] for row in cur.fetchall() if isinstance(row[0], str)]
            target_years = []
            for note in notes:
                match = NOTE_RE.match(note)
                if match:
                    target_years.append(int(match.group(1)))
        else:
            target_years = list(years)

        result: dict[int, list[YearlyTop250Item]] = {}
        for year in target_years:
            note = f"JavDB {year} TOP250"
            cur.execute(
                "SELECT number, name, date, icon_url FROM ranks "
                "WHERE note = ? ORDER BY CAST(number AS INT)",
                (note,),
            )
            items: list[YearlyTop250Item] = []
            for number, name, release_date, icon_url in cur.fetchall():
                try:
                    rank = int(number)
                except (TypeError, ValueError):
                    continue
                code, title = parse_code_and_title(str(name or ""))
                if not code:
                    continue
                items.append(
                    YearlyTop250Item(
                        rank=rank,
                        code=code,
                        title=title,
                        date=str(release_date).strip() if release_date else None,
                        icon_url=str(icon_url).strip() if icon_url else None,
                    )
                )
            result[year] = dedupe_items(items)
        return result
    finally:
        conn.close()


def listing_from_items(
    year: int,
    items: Sequence[YearlyTop250Item],
    *,
    source: str,
    frozen: bool,
    exported_at: str | None = None,
) -> YearlyTop250List:
    cleaned = tuple(dedupe_items(list(items)))
    stamp = exported_at or datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
    return YearlyTop250List(
        slug=yearly_slug(year),
        year=year,
        title=f"JavDB {year} TOP250",
        source=source,
        frozen=frozen,
        exported_at=stamp,
        revision=revision_for_items(cleaned),
        items=cleaned,
    )


def extract_year_from_search_videos(
    videos: Sequence[SearchVideo],
    year: int,
) -> list[YearlyTop250Item]:
    slug = yearly_slug(year)
    rows: list[YearlyTop250Item] = []
    for video in videos:
        position: int | None = None
        for appearance in video.ranking_appearances:
            if appearance.slug == slug:
                position = appearance.position
                break
        if position is None:
            continue
        code = normalize_code(video.code or "") or (video.code or "")
        if not code:
            continue
        rows.append(
            YearlyTop250Item(
                rank=position,
                code=code,
                title=video.title,
                date=video.release_date,
                icon_url=video.cover_url,
            )
        )
    return dedupe_items(rows)


def export_historical_from_jinjier(
    sqlite_path: Path,
    data_dir: Path,
    *,
    force: bool = False,
    years: Sequence[int] | None = None,
    today: date | None = None,
) -> list[YearlyTop250List]:
    """Export completed years from jinjier; never overwrite frozen files unless force."""

    completed = last_completed_year(today=today)
    loaded = load_from_jinjier(sqlite_path, years=years)
    written: list[YearlyTop250List] = []
    for year in sorted(loaded):
        if year > completed:
            # Current year belongs to optional remote refresh path.
            continue
        items = loaded[year]
        if not items:
            continue
        listing = listing_from_items(
            year,
            items,
            source="jinjier",
            frozen=True,
        )
        path = yearly_path(data_dir, year)
        if path.is_file() and not force:
            existing = read_yearly_list(data_dir, year)
            if existing is not None:
                written.append(existing)
                continue
        write_yearly_list(data_dir, listing, force=force)
        written.append(listing)
    return written


def export_year_from_search_index(
    data_dir: Path,
    year: int,
    *,
    force: bool = False,
    frozen: bool | None = None,
    today: date | None = None,
) -> YearlyTop250List | None:
    """Build a yearly file from the on-disk JavRanking search-index.json."""

    index_path = data_dir / "javranking" / "search-index.json"
    if not index_path.is_file():
        return None
    from .javranking_client import parse_search_index

    index = parse_search_index(index_path.read_text(encoding="utf-8"))
    items = extract_year_from_search_videos(index.videos, year)
    if not items:
        return None
    is_frozen = frozen if frozen is not None else year <= last_completed_year(today=today)
    listing = listing_from_items(
        year,
        items,
        source="javranking-index",
        frozen=is_frozen,
    )
    path = yearly_path(data_dir, year)
    if path.is_file() and not force and is_frozen:
        existing = read_yearly_list(data_dir, year)
        if existing is not None:
            return existing
    write_yearly_list(data_dir, listing, force=force or not is_frozen)
    return listing


def sections_from_local_yearly(
    data_dir: Path,
    *,
    today: date | None = None,
) -> list[tuple[str, int, str, int, str | None]]:
    """Return (slug, year, title, item_count, revision) for 2008..current from disk."""

    rows: list[tuple[str, int, str, int, str | None]] = []
    for year in expected_year_range(today=today):
        listing = read_yearly_list(data_dir, year)
        if listing is None:
            # Still advertise the year chip so UI can show empty / pending.
            rows.append((yearly_slug(year), year, f"JavDB {year} TOP250", 0, None))
            continue
        rows.append(
            (
                listing.slug,
                listing.year,
                listing.title,
                len(listing.items),
                listing.revision,
            )
        )
    # Newest first for UI chips
    rows.sort(key=lambda item: -item[1])
    return rows


def parse_year_slug(slug: str) -> int | None:
    text = slug.strip()
    prefix = f"{SLUG_PREFIX}-"
    if not text.startswith(prefix):
        return None
    suffix = text[len(prefix) :]
    if suffix.isdigit():
        return int(suffix)
    return None
