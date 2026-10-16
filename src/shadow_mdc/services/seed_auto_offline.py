"""After daily chart/hot seeds: refresh magnets → pick best (code-match) → OpenList offline.

Used by ``seed_daily_chart`` / ``seed_daily_hot`` (``--auto-offline``) and
``scripts/enqueue_seed_offline.py`` (NAS post-sync / catch-up).
"""

from __future__ import annotations

import asyncio
import json
import logging
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path

from sqlalchemy import select, text

from ..db.models import SourceSnapshot, WorkMagnet
from ..db.repository import Database, Repository
from .discover import DiscoverService
from .library_prefs import LibraryPrefsStore
from .pan import PanService
from .pan_offline_enqueue import (
    OfflineEnqueueError,
    enqueue_work_offline,
    pick_best_magnet,
)

logger = logging.getLogger(__name__)

DEFAULT_PACE_SECONDS = 2.0


@dataclass
class SeedOfflineStats:
    considered: int = 0
    submitted: int = 0
    reused: int = 0
    hunting: int = 0
    want_list_added: int = 0
    skipped_pan: int = 0
    skipped_existing: int = 0
    errors: list[str] = field(default_factory=list)
    # Per-work outcome rows: {"work_id", "code", "outcome", "reason"}.
    results: list[dict[str, object]] = field(default_factory=list)

    def record(self, work_id: str, code: str | None, outcome: str, reason: str = "") -> None:
        self.results.append({"work_id": work_id, "code": code, "outcome": outcome, "reason": reason})

    def as_dict(self) -> dict[str, object]:
        return {
            "considered": self.considered,
            "submitted": self.submitted,
            "reused": self.reused,
            "hunting": self.hunting,
            "want_list_added": self.want_list_added,
            "skipped_pan": self.skipped_pan,
            "skipped_existing": self.skipped_existing,
            "errors": list(self.errors[-20:]),
            "results": list(self.results),
        }


def _javdb_lookup(repo: Repository, work_id: str) -> tuple[str, str | None] | None:
    snapshots = list(
        repo._session.scalars(
            select(SourceSnapshot).where(
                SourceSnapshot.work_id == work_id,
                SourceSnapshot.provider == "javdb",
            )
        )
    )
    if not snapshots:
        return None
    snap = snapshots[0]
    source_url: str | None = None
    if isinstance(snap.payload, dict):
        raw = snap.payload.get("source_url")
        if isinstance(raw, str) and raw.strip():
            source_url = raw.strip()
    return snap.external_id, source_url


def _work_needs_offline(repo: Repository, work_id: str, magnet: WorkMagnet) -> bool:
    existing = repo.find_pan_offline_by_hash(work_id, magnet.info_hash)
    if existing is None:
        return True
    if existing.status == "running":
        return False
    if existing.status in {"done", "completed"}:
        return False
    return True


def _pan_ready(pan: PanService) -> bool:
    cfg = pan.config_store.load()
    status = pan.status()
    if "offline_ready" in status:
        return bool(status.get("connected") and status.get("offline_ready"))
    return bool(status.get("connected") and cfg.offline_directory_id)


def resolve_work_ids_for_day(
    repo: Repository,
    *,
    day: date,
    include_chart: bool = True,
    include_hot: bool = True,
) -> list[str]:
    """Works tagged ``daily-chart-YYYY-MM-DD`` / ``daily-hot-YYYY-MM-DD`` (SQLite json_each)."""

    markers: list[str] = []
    if include_chart:
        markers.append(f"daily-chart-{day.isoformat()}")
    if include_hot:
        markers.append(f"daily-hot-{day.isoformat()}")
    if not markers:
        return []
    # tags is a JSON array column; json_each works on SQLite.
    placeholders = ", ".join(f":m{i}" for i in range(len(markers)))
    params = {f"m{i}": value for i, value in enumerate(markers)}
    rows = repo._session.execute(
        text(
            f"""
            SELECT DISTINCT works.id
            FROM works, json_each(works.tags) AS tag
            WHERE tag.value IN ({placeholders})
            """
        ),
        params,
    ).fetchall()
    return [str(row[0]) for row in rows]


def work_ids_from_run_logs(
    data_dir: Path,
    *,
    day: date,
    created_only: bool = True,
) -> list[str]:
    """Read ``daily-*-runs/{day}.json`` seeded work_ids when present."""

    found: list[str] = []
    seen: set[str] = set()
    for folder in ("daily-chart-runs", "daily-hot-runs"):
        path = data_dir / folder / f"{day.isoformat()}.json"
        if not path.is_file():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            continue
        for item in payload.get("seeded") or []:
            if not isinstance(item, dict):
                continue
            if created_only and not item.get("created"):
                continue
            work_id = item.get("work_id")
            if isinstance(work_id, str) and work_id and work_id != "dry-run" and work_id not in seen:
                seen.add(work_id)
                found.append(work_id)
    return found


def resolve_work_ids_by_codes(repo: Repository, codes: Sequence[str]) -> list[str]:
    ids: list[str] = []
    seen: set[str] = set()
    for raw in codes:
        code = (raw or "").strip()
        if not code:
            continue
        work = repo.find_work_by_code(code)
        if work is None or work.id in seen:
            continue
        seen.add(work.id)
        ids.append(work.id)
    return ids


def _resolve_library_prefs(
    library_prefs_store: LibraryPrefsStore | None,
    *,
    data_dir: Path | None,
) -> LibraryPrefsStore | None:
    if library_prefs_store is not None:
        return library_prefs_store
    if data_dir is not None:
        return LibraryPrefsStore(Path(data_dir) / "library-prefs.json")
    return None


def promote_hunting_to_want_list(
    library_prefs_store: LibraryPrefsStore | None,
    stats: SeedOfflineStats,
) -> int:
    """Auto-add works that finished intake as hunting (no usable magnet) to want_list.

    SubscriptionWatchService already watches want_list, so this keeps retrying
    magnet refresh → offline without a separate queue.
    """

    if library_prefs_store is None:
        return 0
    hunting_ids: list[str] = []
    seen: set[str] = set()
    for row in stats.results:
        if row.get("outcome") != "hunting":
            continue
        work_id = row.get("work_id")
        if not isinstance(work_id, str) or not work_id or work_id in seen:
            continue
        seen.add(work_id)
        hunting_ids.append(work_id)
    if not hunting_ids:
        return 0
    before = set(library_prefs_store.load().want_list)
    library_prefs_store.add_wants(hunting_ids)
    added = [work_id for work_id in hunting_ids if work_id not in before]
    if added:
        stats.want_list_added = len(added)
        logger.info(
            "seed auto-offline: added %s hunting work(s) to want_list",
            len(added),
        )
    return len(added)


async def enqueue_seeded_offline(
    *,
    database: Database,
    pan: PanService,
    discover: DiscoverService,
    work_ids: Sequence[str],
    pace_seconds: float = DEFAULT_PACE_SECONDS,
    library_prefs_store: LibraryPrefsStore | None = None,
    data_dir: Path | None = None,
) -> SeedOfflineStats:
    """Fetch magnets if needed, pick best (code-match preference), submit OpenList offline.

    When intake ends in hunting (no magnet / no suitable magnet), those work_ids
    are auto-added to want_list so SubscriptionWatchService keeps hunting.
    """

    stats = SeedOfflineStats(considered=len(work_ids))
    prefs = _resolve_library_prefs(library_prefs_store, data_dir=data_dir)
    if not work_ids:
        return stats

    pan_ready = _pan_ready(pan)
    if not pan_ready:
        stats.skipped_pan = len(work_ids)
        stats.hunting = len(work_ids)
        for work_id in work_ids:
            stats.record(work_id, None, "skipped", "pan/OpenList offline not ready")
        logger.info("seed auto-offline: pan not ready; skipping %s works", len(work_ids))
        return stats

    pace_next = False
    for work_id in work_ids:
        if pace_next and pace_seconds > 0:
            await asyncio.sleep(pace_seconds)
        pace_next = True
        try:
            touched = await _process_one(
                database=database,
                pan=pan,
                discover=discover,
                work_id=work_id,
                stats=stats,
            )
            pace_next = bool(touched)
        except Exception as exc:  # noqa: BLE001 - continue remaining works
            message = f"{work_id}: {type(exc).__name__}: {exc}"
            stats.errors.append(message)
            stats.record(work_id, None, "error", message)
            logger.warning("seed auto-offline failed: %s", message)
    promote_hunting_to_want_list(prefs, stats)
    return stats


async def _process_one(
    *,
    database: Database,
    pan: PanService,
    discover: DiscoverService,
    work_id: str,
    stats: SeedOfflineStats,
) -> bool:
    """Returns True when the item hit the network (pace the next call)."""

    touched = False
    primary_code: str | None = None
    with database.session() as session:
        repo = Repository(session)
        work = repo.get_work(work_id)
        if work is None:
            stats.errors.append(f"{work_id}: work not found")
            stats.record(work_id, None, "error", "work not found")
            return False
        primary_code = work.primary_code
        magnets = list(repo.list_work_magnets(work_id))
        lookup = _javdb_lookup(repo, work_id) if not magnets else None

    if not magnets and lookup is not None:
        touched = True
        external_id, source_url = lookup
        try:
            fetched = await discover.list_magnets(
                provider="javdb",
                external_id=external_id,
                source_url=source_url,
            )
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{work_id}: magnet refresh {type(exc).__name__}")
            fetched = ()
        if fetched:
            with database.session() as session:
                repo = Repository(session)
                work = repo.get_work(work_id)
                if work is None:
                    return touched
                payload = [item.model_dump(mode="json") for item in fetched]
                created, _skipped = repo.save_work_magnets(work, payload, provider="javdb")
                magnets = list(repo.list_work_magnets(work_id))
                if created:
                    logger.info(
                        "seed auto-offline magnets saved work=%s count=%s",
                        work_id,
                        created,
                    )

    if not magnets and discover.sukebei_available and primary_code:
        touched = True
        try:
            fetched = await discover.list_magnets(provider="sukebei", external_id=primary_code)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"{work_id}: sukebei magnets {type(exc).__name__}")
            fetched = ()
        if fetched:
            with database.session() as session:
                repo = Repository(session)
                work = repo.get_work(work_id)
                if work is None:
                    return touched
                payload = [item.model_dump(mode="json") for item in fetched]
                repo.save_work_magnets(work, payload, provider="sukebei")
                magnets = list(repo.list_work_magnets(work_id))

    if not magnets:
        stats.hunting += 1
        stats.record(work_id, primary_code, "hunting", "no magnets found (javdb/sukebei)")
        return touched

    best = pick_best_magnet(magnets, expected_code=primary_code)
    if best is None:
        stats.hunting += 1
        stats.record(work_id, primary_code, "hunting", "no suitable magnet")
        return touched

    with database.session() as session:
        repo = Repository(session)
        if not _work_needs_offline(repo, work_id, best):
            stats.skipped_existing += 1
            stats.record(work_id, primary_code, "skipped", "offline task already running/done")
            return touched

    touched = True
    with database.session() as session:
        repo = Repository(session)
        try:
            result = await enqueue_work_offline(repo, pan, work_id, magnet_id=best.id)
        except OfflineEnqueueError as exc:
            stats.errors.append(f"{work_id}: {exc.message}")
            if exc.status_code == 400:
                stats.skipped_pan += 1
            stats.hunting += 1
            stats.record(work_id, primary_code, "error", exc.message)
            return touched
        if result.reused_running:
            stats.reused += 1
            stats.record(work_id, primary_code, "reused", "matching offline task already running")
        else:
            stats.submitted += 1
            stats.record(work_id, primary_code, "submitted", best.info_hash[:12])
        logger.info(
            "seed auto-offline enqueued work=%s code=%s hash=%s created=%s",
            work_id,
            primary_code,
            best.info_hash[:12],
            result.created,
        )
    return touched


def collect_new_seed_work_ids(
    *,
    seeded: Sequence[object],
    created_only: bool = True,
) -> list[str]:
    """Pull work_ids from SeededWorkSummary-like objects (``created`` / ``work_id``)."""

    out: list[str] = []
    seen: set[str] = set()
    for item in seeded:
        created = bool(getattr(item, "created", False))
        if created_only and not created:
            continue
        work_id = getattr(item, "work_id", None)
        if isinstance(work_id, str) and work_id and work_id != "dry-run" and work_id not in seen:
            seen.add(work_id)
            out.append(work_id)
    return out


def work_codes_from_bundle(bundle_dir: Path) -> list[str]:
    """Primary codes of the works carried by a catalog bundle (``export_catalog_bundle``).

    Work ids are remapped on import, so the NAS must re-resolve by code. Uses
    ``manifest.incremental.works`` when present, else every work in the bundle DB.
    """

    bundle_dir = Path(bundle_dir)
    db_path = bundle_dir / "data" / "shadow-mdc.db"
    if not db_path.is_file():
        return []
    wanted: list[str] | None = None
    manifest = bundle_dir / "manifest.json"
    if manifest.is_file():
        try:
            payload = json.loads(manifest.read_text(encoding="utf-8"))
            incremental = payload.get("incremental")
            if isinstance(incremental, dict) and isinstance(incremental.get("works"), list):
                wanted = [str(item) for item in incremental["works"]]
        except (OSError, UnicodeError, ValueError):
            wanted = None
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute("SELECT id, primary_code FROM works").fetchall()
    finally:
        conn.close()
    wanted_set = set(wanted) if wanted is not None else None
    codes: list[str] = []
    seen: set[str] = set()
    for work_id, code in rows:
        if wanted_set is not None and str(work_id) not in wanted_set:
            continue
        if not code or code in seen:
            continue
        seen.add(code)
        codes.append(str(code))
    return codes


def resolve_work_ids_created_on(repo: Repository, *, day: date) -> list[str]:
    """Works whose ``created_at`` (stored UTC) falls on local ``day`` (box/NAS tz)."""

    start_local = datetime.combine(day, time()).astimezone()
    start = start_local.astimezone(UTC).replace(tzinfo=None)
    end = (start_local + timedelta(days=1)).astimezone(UTC).replace(tzinfo=None)
    rows = repo._session.execute(
        text("SELECT id FROM works WHERE created_at >= :start AND created_at < :end ORDER BY created_at"),
        {"start": start.isoformat(sep=" "), "end": end.isoformat(sep=" ")},
    ).fetchall()
    return [str(row[0]) for row in rows]


async def auto_offline_new_works(
    *,
    settings: object,
    database: Database,
    discover: DiscoverService,
    work_ids: Sequence[str],
    pace_seconds: float = DEFAULT_PACE_SECONDS,
) -> SeedOfflineStats | None:
    """Shared post-seed hook for every intake script (chart/hot/VR/manual codes)."""

    ids = [item for item in work_ids if item and item != "dry-run"]
    if not ids:
        return None
    data_dir = getattr(settings, "data_dir")
    pan = PanService(
        data_dir=data_dir,
        proxy_url=getattr(settings, "proxy_url", None),
    )
    return await enqueue_seeded_offline(
        database=database,
        pan=pan,
        discover=discover,
        work_ids=ids,
        pace_seconds=pace_seconds,
        data_dir=Path(data_dir) if data_dir is not None else None,
    )
