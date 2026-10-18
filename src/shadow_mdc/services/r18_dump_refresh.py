"""Weekly r18.dev dump refresh + update-only catalog enrich (NAS job helpers).

The production pipeline stops the API (SQLite single-writer), refreshes the dump,
optionally enriches existing JAV works, then starts the API again. See
``scripts/run_r18_dump_weekly.py`` and ``scripts/nas/r18_dump_weekly.sh``.
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import Settings
from ..db.models import Work
from ..db.repository import Database, Repository
from ..enums import ContentFamily
from ..identity import extract_code
from .r18_dump import ImportStats, R18DumpStore, import_dump_to_sqlite
from .r18_dump_download import (
    LATEST_DUMP_URL,
    NetworkStallWatch,
    R18DumpDownloadError,
    R18DumpDownloadResult,
    download_latest_dump,
)

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_CRON = "1 10 * * 3"
DEFAULT_REFRESH_TIMEZONE = "Asia/Shanghai"


@dataclass(frozen=True)
class JavCatalogGapCounts:
    """Catalog snapshot used before/after weekly enrich (jav_total must not grow)."""

    jav_total: int
    missing_actress: int
    missing_maker: int
    missing_release_date: int
    missing_duration: int

    def as_dict(self) -> dict[str, int]:
        return {
            "jav_total": self.jav_total,
            "missing_actress": self.missing_actress,
            "missing_maker": self.missing_maker,
            "missing_release_date": self.missing_release_date,
            "missing_duration": self.missing_duration,
        }


@dataclass(frozen=True)
class R18DumpRefreshOutcome:
    """Result of download (+ optional SQLite import)."""

    dump_path: Path
    sqlite_path: Path
    download: R18DumpDownloadResult
    imported: bool
    import_stats: ImportStats | None


@dataclass(frozen=True)
class R18DumpEnrichOutcome:
    candidates: int
    matched: int
    updated: int
    skipped: int


def dump_dir(settings: Settings) -> Path:
    path = settings.data_dir / "r18-dumps"
    path.mkdir(parents=True, exist_ok=True)
    return path


def count_jav_catalog_gaps(session: Session) -> JavCatalogGapCounts:
    """Count JAV works and common metadata gaps (actress / maker / date / duration)."""

    works = list(session.scalars(select(Work).where(Work.family == ContentFamily.JAV.value)))
    missing_actress = 0
    missing_maker = 0
    missing_release_date = 0
    missing_duration = 0
    for work in works:
        actors = work.actors if isinstance(work.actors, list) else []
        if not actors:
            missing_actress += 1
        if not (work.studio or "").strip():
            missing_maker += 1
        if work.release_date is None:
            missing_release_date += 1
        if not work.runtime_seconds:
            missing_duration += 1
    return JavCatalogGapCounts(
        jav_total=len(works),
        missing_actress=missing_actress,
        missing_maker=missing_maker,
        missing_release_date=missing_release_date,
        missing_duration=missing_duration,
    )


def count_jav_catalog_gaps_from_database(database: Database) -> JavCatalogGapCounts:
    with database.session() as session:
        return count_jav_catalog_gaps(session)


def resolve_proxy(settings: Settings, proxy: str | None = None) -> str | None:
    """Prefer an explicit proxy, else Settings.proxy_url (SHADOW_MDC_PROXY_URL)."""

    if proxy is not None and proxy.strip():
        return proxy.strip()
    if settings.proxy_url and settings.proxy_url.strip():
        return settings.proxy_url.strip()
    return None


def refresh_r18_dump(
    settings: Settings,
    *,
    proxy: str | None = None,
    progress: Callable[[int, int | None], None] | None = None,
    progress_every: int = 50_000,
    stall_limit_seconds: float = 300.0,
    force_import: bool = False,
) -> R18DumpRefreshOutcome:
    """Download the latest dump and import to SQLite when the dump file changed.

    When the dated dump is already on disk (``unchanged``) and the SQLite DB
    exists, import is skipped unless ``force_import`` is set.
    """

    settings.ensure_directories()
    target = dump_dir(settings)
    sqlite_path = settings.resolved_r18_dump_db()
    stall = NetworkStallWatch(limit_seconds=stall_limit_seconds)
    timeout = httpx.Timeout(60.0, read=600.0)
    resolved_proxy = resolve_proxy(settings, proxy)
    logger.info(
        "r18 dump refresh start dir=%s proxy=%s sqlite=%s",
        target,
        "set" if resolved_proxy else "none",
        sqlite_path,
    )
    with httpx.Client(proxy=resolved_proxy, timeout=timeout) as client:
        try:
            download = download_latest_dump(
                target,
                client=client,
                progress=progress,
                on_eof_probe_start=stall.pause,
                on_eof_probe_done=stall.resume,
                on_network_byte=stall.beat,
            )
        except R18DumpDownloadError:
            logger.exception("r18 dump download failed")
            raise
    skip_import = download.unchanged and sqlite_path.is_file() and not force_import
    import_stats: ImportStats | None = None
    imported = False
    if skip_import:
        logger.info(
            "r18 dump unchanged path=%s date=%s; skipping SQLite import",
            download.path,
            download.source_date or "unknown",
        )
    else:
        logger.info("r18 dump import start %s → %s", download.path, sqlite_path)
        import_stats = import_dump_to_sqlite(
            download.path,
            sqlite_path,
            progress_every=progress_every,
            on_import_start=stall.pause,
            on_import_done=stall.resume,
        )
        imported = True
        logger.info(
            "r18 dump import success videos=%s actresses=%s",
            import_stats.videos,
            import_stats.actresses,
        )
    return R18DumpRefreshOutcome(
        dump_path=download.path,
        sqlite_path=sqlite_path,
        download=download,
        imported=imported,
        import_stats=import_stats,
    )


def enrich_existing_jav_from_dump(
    settings: Settings,
    *,
    sqlite_path: Path | None = None,
    limit: int = 0,
    offset: int = 0,
    dry_run: bool = False,
    commit_every: int = 50,
) -> R18DumpEnrichOutcome:
    """Update existing JAV works from the dump SQLite. Never creates works."""

    settings.ensure_directories()
    dump_sqlite = Path(sqlite_path) if sqlite_path is not None else settings.resolved_r18_dump_db()
    if not dump_sqlite.is_file():
        raise FileNotFoundError(f"r18 dump sqlite missing: {dump_sqlite}")

    database = Database(settings.database_url)
    database.initialize()
    matched = 0
    updated = 0
    skipped = 0
    track_fields = (
        "title",
        "original_title",
        "actors",
        "release_date",
        "runtime_seconds",
        "studio",
        "label",
        "series",
        "plot",
        "original_plot",
        "tags",
        "artwork",
    )

    def field_present(work: Work, name: str) -> bool:
        if name == "title":
            return bool((work.title or "").strip())
        if name == "original_title":
            return bool((work.original_title or "").strip())
        if name == "actors":
            return bool(work.actors)
        if name == "release_date":
            return work.release_date is not None
        if name == "runtime_seconds":
            return bool(work.runtime_seconds)
        if name == "studio":
            return bool(work.studio)
        if name == "label":
            return bool(work.label)
        if name == "series":
            return bool(work.series)
        if name == "plot":
            return bool((work.plot or "").strip())
        if name == "original_plot":
            return bool((work.original_plot or "").strip())
        if name == "tags":
            return bool(work.tags)
        if name == "artwork":
            art = work.artwork if isinstance(work.artwork, list) else []
            return len(art) > 0
        return False

    def gaps(work: Work) -> list[str]:
        return [name for name in track_fields if not field_present(work, name)]

    logger.info("r18 dump enrich start sqlite=%s dry_run=%s", dump_sqlite, dry_run)
    with database.session() as session, R18DumpStore(dump_sqlite) as store:
        repo = Repository(session)
        works = [work for work in repo.list_works() if work.family == ContentFamily.JAV.value]
        works = [work for work in works if gaps(work)]
        if offset:
            works = works[offset:]
        if limit:
            works = works[:limit]
        candidates = len(works)
        pending_commit = 0
        for work in works:
            raw_code = work.primary_code or ""
            code, family = extract_code(raw_code)
            if code is None or family is not ContentFamily.JAV:
                skipped += 1
                continue
            before = set(gaps(work))
            record = store.build_record(code)
            if record is None:
                skipped += 1
                continue
            matched += 1
            if dry_run:
                continue
            repo.merge_provider_into_work(work, record, overwrite=False)
            if not (work.original_plot or "").strip() and record.plot and record.language == "ja":
                work.original_plot = record.plot
                sources = dict(work.field_sources or {})
                sources.setdefault("original_plot", record.provider)
                work.field_sources = sources
            after = set(gaps(work))
            if before - after:
                updated += 1
            pending_commit += 1
            if pending_commit >= max(1, commit_every):
                session.commit()
                pending_commit = 0
        if pending_commit and not dry_run:
            session.commit()
    logger.info(
        "r18 dump enrich success candidates=%s matched=%s updated=%s skipped=%s",
        candidates,
        matched,
        updated,
        skipped,
    )
    return R18DumpEnrichOutcome(
        candidates=candidates,
        matched=matched,
        updated=updated,
        skipped=skipped,
    )


def assert_enrich_did_not_grow_catalog(
    before: JavCatalogGapCounts,
    after: JavCatalogGapCounts,
) -> None:
    """Enrich must update existing works only — jav_total must not increase."""

    if after.jav_total > before.jav_total:
        raise RuntimeError(
            f"r18 dump enrich increased jav_total ({before.jav_total} → {after.jav_total}); "
            "aborting (update-only invariant broken)"
        )


__all__ = [
    "DEFAULT_REFRESH_CRON",
    "DEFAULT_REFRESH_TIMEZONE",
    "LATEST_DUMP_URL",
    "JavCatalogGapCounts",
    "R18DumpEnrichOutcome",
    "R18DumpRefreshOutcome",
    "assert_enrich_did_not_grow_catalog",
    "count_jav_catalog_gaps",
    "count_jav_catalog_gaps_from_database",
    "dump_dir",
    "enrich_existing_jav_from_dump",
    "refresh_r18_dump",
    "resolve_proxy",
]
