"""Offline r18.dev PostgreSQL dump → SQLite lookup (structured data is CC0).

Weekly dumps: https://r18.dev/dumps . Keep mirrors on NAS only; never commit dump
files or the SQLite DB to git.
"""

from __future__ import annotations

import gzip
import re
import sqlite3
from collections.abc import Iterable, Iterator, Sequence
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

from ..dmm_ids import content_id_candidates
from ..domain import Artwork, ProviderRecord
from ..enums import ContentFamily
from ..identity import extract_code
from ..normalize_code import normalize_code, to_comparison_key

PROVIDER_ID = "r18dump"
DMM_PICS_BASE = "https://pics.dmm.co.jp/"

_TABLE_MAP: dict[str, str] = {
    "derived_video": "videos",
    "derived_actress": "actresses",
    "derived_actor": "actors",
    "derived_director": "directors",
    "derived_maker": "makers",
    "derived_label": "labels",
    "derived_series": "series",
    "derived_category": "categories",
    "derived_video_actress": "video_actresses",
    "derived_video_actor": "video_actors",
    "derived_video_director": "video_directors",
    "derived_video_category": "video_categories",
}

_SCHEMA_SQL = """
PRAGMA journal_mode = WAL;
PRAGMA synchronous = NORMAL;
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY,
  value TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS videos (
  content_id TEXT PRIMARY KEY,
  dvd_id TEXT,
  dvd_id_norm TEXT,
  dvd_id_key TEXT,
  title_en TEXT,
  title_ja TEXT,
  comment_en TEXT,
  comment_ja TEXT,
  runtime_mins INTEGER,
  release_date TEXT,
  sample_url TEXT,
  maker_id INTEGER,
  label_id INTEGER,
  series_id INTEGER,
  jacket_full_url TEXT,
  jacket_thumb_url TEXT,
  gallery_full_first TEXT,
  gallery_full_last TEXT,
  gallery_thumb_first TEXT,
  gallery_thumb_last TEXT,
  site_id INTEGER,
  service_code TEXT
);
CREATE TABLE IF NOT EXISTS actresses (
  id INTEGER PRIMARY KEY,
  name_romaji TEXT,
  image_url TEXT,
  name_kanji TEXT,
  name_kana TEXT
);
CREATE TABLE IF NOT EXISTS actors (
  id INTEGER PRIMARY KEY,
  name_kanji TEXT,
  name_kana TEXT
);
CREATE TABLE IF NOT EXISTS directors (
  id INTEGER PRIMARY KEY,
  name_kanji TEXT,
  name_kana TEXT,
  name_romaji TEXT
);
CREATE TABLE IF NOT EXISTS makers (
  id INTEGER PRIMARY KEY,
  name_en TEXT,
  name_ja TEXT
);
CREATE TABLE IF NOT EXISTS labels (
  id INTEGER PRIMARY KEY,
  name_en TEXT,
  name_ja TEXT
);
CREATE TABLE IF NOT EXISTS series (
  id INTEGER PRIMARY KEY,
  name_en TEXT,
  name_ja TEXT
);
CREATE TABLE IF NOT EXISTS categories (
  id INTEGER PRIMARY KEY,
  name_en TEXT,
  name_ja TEXT
);
CREATE TABLE IF NOT EXISTS video_actresses (
  content_id TEXT NOT NULL,
  actress_id INTEGER NOT NULL,
  ordinality INTEGER,
  PRIMARY KEY (content_id, actress_id)
);
CREATE TABLE IF NOT EXISTS video_actors (
  content_id TEXT NOT NULL,
  actor_id INTEGER NOT NULL,
  ordinality INTEGER,
  PRIMARY KEY (content_id, actor_id)
);
CREATE TABLE IF NOT EXISTS video_directors (
  content_id TEXT NOT NULL,
  director_id INTEGER NOT NULL,
  PRIMARY KEY (content_id, director_id)
);
CREATE TABLE IF NOT EXISTS video_categories (
  content_id TEXT NOT NULL,
  category_id INTEGER NOT NULL,
  PRIMARY KEY (content_id, category_id)
);
CREATE INDEX IF NOT EXISTS ix_videos_dvd_id_key ON videos(dvd_id_key);
CREATE INDEX IF NOT EXISTS ix_videos_dvd_id_norm ON videos(dvd_id_norm);
CREATE INDEX IF NOT EXISTS ix_actresses_romaji ON actresses(name_romaji);
CREATE INDEX IF NOT EXISTS ix_actresses_kanji ON actresses(name_kanji);
"""

_COPY_RE = re.compile(
    r"^COPY public\.(?P<table>\w+) \((?P<cols>[^)]+)\) FROM stdin;\s*$"
)
_GALLERY_INDEX_RE = re.compile(r"^(?P<prefix>.*?)(?P<num>\d+)$")


def absolute_dmm_image(path: str | None) -> str | None:
    if not path:
        return None
    value = path.strip()
    if not value or value == "\\N":
        return None
    if value.startswith(("http://", "https://")):
        return value
    if "/" not in value:
        return f"{DMM_PICS_BASE}mono/actjpgs/{value}.jpg"
    if not re.search(r"\.(jpg|jpeg|png|webp)$", value, re.I):
        value = f"{value}.jpg"
    return f"{DMM_PICS_BASE}{value.lstrip('/')}"


def expand_gallery(first: str | None, last: str | None, *, limit: int = 60) -> list[str]:
    if not first:
        return []
    if not last or first == last:
        return [first]
    match_first = _GALLERY_INDEX_RE.match(first)
    match_last = _GALLERY_INDEX_RE.match(last)
    if match_first is None or match_last is None:
        return [first, last]
    if match_first.group("prefix") != match_last.group("prefix"):
        return [first, last]
    start = int(match_first.group("num"))
    end = int(match_last.group("num"))
    if end < start or end - start + 1 > limit:
        return [first, last]
    width = len(match_first.group("num"))
    prefix = match_first.group("prefix")
    if match_first.group("num").startswith("0"):
        return [f"{prefix}{index:0{width}d}" for index in range(start, end + 1)]
    return [f"{prefix}{index}" for index in range(start, end + 1)]


def _pg_value(raw: str) -> str | None:
    if raw == "\\N":
        return None
    return (
        raw.replace("\\t", "\t")
        .replace("\\n", "\n")
        .replace("\\r", "\r")
        .replace("\\\\", "\\")
    )


def _open_dump(path: Path):
    if str(path).endswith(".gz"):
        return gzip.open(path, "rt", encoding="utf-8", errors="replace", newline="\n")
    return path.open("rt", encoding="utf-8", errors="replace", newline="\n")


def iter_copy_rows(
    path: Path, tables: set[str]
) -> Iterator[tuple[str, list[str], list[str | None]]]:
    wanted = set(tables)
    with _open_dump(path) as handle:
        current_table: str | None = None
        columns: list[str] = []
        for line in handle:
            if current_table is None:
                match = _COPY_RE.match(line)
                if match and match.group("table") in wanted:
                    current_table = match.group("table")
                    columns = [part.strip() for part in match.group("cols").split(",")]
                continue
            if line.startswith("\\."):
                current_table = None
                columns = []
                continue
            raw = line[:-1] if line.endswith("\n") else line
            parts = raw.split("\t")
            if len(parts) != len(columns):
                continue
            yield current_table, columns, [_pg_value(part) for part in parts]


def _row_dict(columns: Sequence[str], values: Sequence[str | None]) -> dict[str, str | None]:
    return {column: values[index] for index, column in enumerate(columns)}


def _dvd_keys(dvd_id: str | None) -> tuple[str | None, str | None]:
    if not dvd_id:
        return None, None
    code, _family = extract_code(dvd_id)
    seed = code or dvd_id
    normalized = normalize_code(seed) or None
    key = to_comparison_key(seed) or None
    return normalized, key


def _as_int(value: str | None) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except ValueError:
        return None


@dataclass(slots=True)
class ImportStats:
    source: str
    tables: dict[str, int] = field(default_factory=dict)
    videos: int = 0
    actresses: int = 0


def import_dump_to_sqlite(
    dump_path: Path,
    sqlite_path: Path,
    *,
    progress_every: int = 50_000,
) -> ImportStats:
    """Stream a gzipped pg_dump into a replacement SQLite lookup DB."""

    dump_path = Path(dump_path)
    sqlite_path = Path(sqlite_path)
    sqlite_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = sqlite_path.with_suffix(sqlite_path.suffix + ".tmp")
    if temporary.exists():
        temporary.unlink()

    connection = sqlite3.connect(temporary)
    counts = {name: 0 for name in _TABLE_MAP}
    batch: dict[str, list[tuple[Any, ...]]] = {name: [] for name in _TABLE_MAP.values()}
    batch_limit = 2000

    def flush(table: str) -> None:
        rows = batch[table]
        if not rows:
            return
        placeholders = ",".join("?" for _ in rows[0])
        connection.executemany(
            f"INSERT OR REPLACE INTO {table} VALUES ({placeholders})",
            rows,
        )
        rows.clear()

    try:
        connection.executescript(_SCHEMA_SQL)
        for pg_table, columns, values in iter_copy_rows(dump_path, set(_TABLE_MAP)):
            sqlite_table = _TABLE_MAP[pg_table]
            row = _row_dict(columns, values)
            if sqlite_table == "videos":
                content_id = row.get("content_id")
                if not content_id:
                    continue
                dvd_id = row.get("dvd_id")
                dvd_norm, dvd_key = _dvd_keys(dvd_id)
                batch[sqlite_table].append(
                    (
                        content_id,
                        dvd_id,
                        dvd_norm,
                        dvd_key,
                        row.get("title_en"),
                        row.get("title_ja"),
                        row.get("comment_en"),
                        row.get("comment_ja"),
                        _as_int(row.get("runtime_mins")),
                        row.get("release_date"),
                        row.get("sample_url"),
                        _as_int(row.get("maker_id")),
                        _as_int(row.get("label_id")),
                        _as_int(row.get("series_id")),
                        row.get("jacket_full_url"),
                        row.get("jacket_thumb_url"),
                        row.get("gallery_full_first"),
                        row.get("gallery_full_last"),
                        row.get("gallery_thumb_first"),
                        row.get("gallery_thumb_last"),
                        _as_int(row.get("site_id")),
                        row.get("service_code"),
                    )
                )
            elif sqlite_table == "actresses":
                actress_id = _as_int(row.get("id"))
                if actress_id is None:
                    continue
                batch[sqlite_table].append(
                    (
                        actress_id,
                        row.get("name_romaji"),
                        row.get("image_url"),
                        row.get("name_kanji"),
                        row.get("name_kana"),
                    )
                )
            elif sqlite_table == "actors":
                actor_id = _as_int(row.get("id"))
                if actor_id is None:
                    continue
                batch[sqlite_table].append(
                    (actor_id, row.get("name_kanji"), row.get("name_kana"))
                )
            elif sqlite_table == "directors":
                director_id = _as_int(row.get("id"))
                if director_id is None:
                    continue
                batch[sqlite_table].append(
                    (
                        director_id,
                        row.get("name_kanji"),
                        row.get("name_kana"),
                        row.get("name_romaji"),
                    )
                )
            elif sqlite_table in {"makers", "labels", "series", "categories"}:
                entity_id = _as_int(row.get("id"))
                if entity_id is None:
                    continue
                batch[sqlite_table].append(
                    (entity_id, row.get("name_en"), row.get("name_ja"))
                )
            elif sqlite_table == "video_actresses":
                if not row.get("content_id") or _as_int(row.get("actress_id")) is None:
                    continue
                batch[sqlite_table].append(
                    (
                        row.get("content_id"),
                        _as_int(row.get("actress_id")),
                        _as_int(row.get("ordinality")),
                    )
                )
            elif sqlite_table == "video_actors":
                if not row.get("content_id") or _as_int(row.get("actor_id")) is None:
                    continue
                batch[sqlite_table].append(
                    (
                        row.get("content_id"),
                        _as_int(row.get("actor_id")),
                        _as_int(row.get("ordinality")),
                    )
                )
            elif sqlite_table == "video_directors":
                if not row.get("content_id") or _as_int(row.get("director_id")) is None:
                    continue
                batch[sqlite_table].append(
                    (row.get("content_id"), _as_int(row.get("director_id")))
                )
            elif sqlite_table == "video_categories":
                if not row.get("content_id") or _as_int(row.get("category_id")) is None:
                    continue
                batch[sqlite_table].append(
                    (row.get("content_id"), _as_int(row.get("category_id")))
                )

            counts[pg_table] += 1
            if len(batch[sqlite_table]) >= batch_limit:
                flush(sqlite_table)
            if progress_every and counts[pg_table] % progress_every == 0:
                print(f"  … {pg_table}: {counts[pg_table]:,}", flush=True)

        for table_name in batch:
            flush(table_name)
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("source_dump", dump_path.name),
        )
        connection.execute(
            "INSERT OR REPLACE INTO meta(key, value) VALUES (?, ?)",
            ("video_count", str(counts.get("derived_video", 0))),
        )
        connection.commit()
    finally:
        connection.close()

    temporary.replace(sqlite_path)
    return ImportStats(
        source=str(dump_path),
        tables={_TABLE_MAP[key]: value for key, value in counts.items()},
        videos=counts.get("derived_video", 0),
        actresses=counts.get("derived_actress", 0),
    )


@dataclass(slots=True)
class ActressRow:
    id: int
    name_kanji: str | None
    name_romaji: str | None
    name_kana: str | None
    image_url: str | None

    @property
    def display_name(self) -> str | None:
        for value in (self.name_kanji, self.name_romaji, self.name_kana):
            if value and value.strip():
                return value.strip()
        return None

    def aliases(self) -> tuple[str, ...]:
        names: list[str] = []
        for value in (self.name_kanji, self.name_romaji, self.name_kana):
            cleaned = (value or "").strip()
            if cleaned and cleaned not in names:
                names.append(cleaned)
        return tuple(names)


class R18DumpStore:
    """Read-only lookup against an imported r18.dev SQLite dump."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self._connection = sqlite3.connect(
            f"file:{self.path}?mode=ro",
            uri=True,
            check_same_thread=False,
        )
        self._connection.row_factory = sqlite3.Row

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> R18DumpStore:
        return self

    def __exit__(self, *args: object) -> None:
        self.close()

    def meta(self) -> dict[str, str]:
        rows = self._connection.execute("SELECT key, value FROM meta").fetchall()
        return {str(row["key"]): str(row["value"]) for row in rows}

    def find_video_row(self, code: str) -> sqlite3.Row | None:
        key = to_comparison_key(code)
        norm = normalize_code(code)
        if key:
            row = self._connection.execute(
                "SELECT * FROM videos WHERE dvd_id_key = ? LIMIT 1",
                (key,),
            ).fetchone()
            if row is not None:
                return row
        if norm:
            row = self._connection.execute(
                "SELECT * FROM videos WHERE dvd_id_norm = ? LIMIT 1",
                (norm,),
            ).fetchone()
            if row is not None:
                return row
        for candidate in content_id_candidates(code):
            row = self._connection.execute(
                "SELECT * FROM videos WHERE content_id = ? LIMIT 1",
                (candidate,),
            ).fetchone()
            if row is not None:
                return row
        return None

    def actresses_for(self, content_id: str) -> list[ActressRow]:
        rows = self._connection.execute(
            """
            SELECT a.id, a.name_kanji, a.name_romaji, a.name_kana, a.image_url
            FROM video_actresses va
            JOIN actresses a ON a.id = va.actress_id
            WHERE va.content_id = ?
            ORDER BY COALESCE(va.ordinality, 9999), a.id
            """,
            (content_id,),
        ).fetchall()
        return [
            ActressRow(
                id=int(row["id"]),
                name_kanji=row["name_kanji"],
                name_romaji=row["name_romaji"],
                name_kana=row["name_kana"],
                image_url=row["image_url"],
            )
            for row in rows
        ]

    def directors_for(self, content_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT d.name_kanji, d.name_romaji, d.name_kana
            FROM video_directors vd
            JOIN directors d ON d.id = vd.director_id
            WHERE vd.content_id = ?
            """,
            (content_id,),
        ).fetchall()
        names: list[str] = []
        for row in rows:
            for key in ("name_kanji", "name_romaji", "name_kana"):
                value = row[key]
                if value and str(value).strip() and str(value).strip() not in names:
                    names.append(str(value).strip())
                    break
        return names

    def categories_for(self, content_id: str) -> list[str]:
        rows = self._connection.execute(
            """
            SELECT c.name_ja, c.name_en
            FROM video_categories vc
            JOIN categories c ON c.id = vc.category_id
            WHERE vc.content_id = ?
            """,
            (content_id,),
        ).fetchall()
        names: list[str] = []
        for row in rows:
            for key in ("name_ja", "name_en"):
                value = row[key]
                if value and str(value).strip() and str(value).strip() not in names:
                    names.append(str(value).strip())
                    break
        return names

    def _named(self, table: str, entity_id: int | None) -> str | None:
        if entity_id is None:
            return None
        row = self._connection.execute(
            f"SELECT name_ja, name_en FROM {table} WHERE id = ?",
            (entity_id,),
        ).fetchone()
        if row is None:
            return None
        return row["name_ja"] or row["name_en"] or None

    def build_record(self, code: str) -> ProviderRecord | None:
        row = self.find_video_row(code)
        if row is None:
            return None
        dvd_id = row["dvd_id"] or code
        parsed, family = extract_code(str(dvd_id))
        if parsed is None or family is not ContentFamily.JAV:
            parsed = normalize_code(str(dvd_id)) or code
        title_ja = row["title_ja"]
        title_en = row["title_en"]
        title = title_ja or title_en
        if not title:
            return None
        content_id = str(row["content_id"])
        actresses = self.actresses_for(content_id)
        actor_names = tuple(
            name for actress in actresses if (name := actress.display_name) is not None
        )
        plot = row["comment_ja"] or row["comment_en"]
        runtime = row["runtime_mins"]
        release_date = None
        if row["release_date"]:
            try:
                release_date = date.fromisoformat(str(row["release_date"])[:10])
            except ValueError:
                release_date = None

        artwork_items: list[Artwork] = []
        seen: set[str] = set()

        def add(url: str | None, kind: str) -> None:
            absolute = absolute_dmm_image(url)
            if absolute and absolute not in seen:
                seen.add(absolute)
                artwork_items.append(Artwork.model_validate({"url": absolute, "kind": kind}))

        add(row["jacket_full_url"], "fanart")
        add(row["jacket_thumb_url"], "poster")
        for gallery_path in expand_gallery(row["gallery_full_first"], row["gallery_full_last"]):
            add(gallery_path, "sample")

        return ProviderRecord(
            provider=PROVIDER_ID,
            external_id=content_id,
            source_url=f"https://r18.dev/videos/vod/movies/detail/-/id={content_id}",
            code=parsed,
            title=str(title),
            original_title=str(title_ja) if title_ja else str(title),
            family=ContentFamily.JAV,
            release_date=release_date,
            runtime_seconds=int(runtime) * 60 if runtime is not None else None,
            studio=self._named("makers", row["maker_id"]),
            label=self._named("labels", row["label_id"]),
            series=self._named("series", row["series_id"]),
            plot=str(plot) if plot else None,
            actors=actor_names,
            directors=tuple(self.directors_for(content_id)),
            tags=tuple(self.categories_for(content_id)),
            artwork=tuple(artwork_items),
            language="ja" if title_ja else "en",
        )

    def iter_actress_alias_pairs(self) -> Iterable[tuple[str, str]]:
        """Yield (alias, canonical_japanese) for romaji→kanji enrichment."""

        rows = self._connection.execute(
            """
            SELECT name_romaji, name_kanji, name_kana
            FROM actresses
            WHERE name_kanji IS NOT NULL AND TRIM(name_kanji) != ''
            """
        )
        for row in rows:
            kanji = str(row["name_kanji"]).strip()
            for alias in (row["name_romaji"], row["name_kana"]):
                if alias and str(alias).strip() and str(alias).strip() != kanji:
                    yield str(alias).strip(), kanji
