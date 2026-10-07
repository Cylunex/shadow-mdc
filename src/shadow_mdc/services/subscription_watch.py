"""Background watcher: subscribed/want works → refresh magnets → optional offline (115 or OpenList)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from ..db.models import SourceSnapshot, Work, WorkMagnet
from ..db.repository import Database, Repository
from .discover import DiscoverService
from .library_prefs import LibraryPrefs, LibraryPrefsStore
from .pan import PanService
from .pan_offline_enqueue import (
    OfflineEnqueueError,
    enqueue_work_offline,
    pick_best_magnet,
)
from .subscriptions import filter_works_for_subscription, initialize_subscription_cursor
from .task_events import TaskEventHub

logger = logging.getLogger(__name__)

WATCH_IDLE_SECONDS = 15 * 60
# Page size while draining the due backlog (checkpoint is saved per item and
# progress is published per page). Each run drains *every* due target.
WATCH_BATCH_SIZE = 25
WATCH_PACE_SECONDS = 2.0
# A target is due again once its last check is at least this old at the run's
# cutoff. Slightly below the idle interval so every tick re-checks everything
# that was not touched by an interrupted / overlapping run.
WATCH_RECHECK_SECONDS = 10 * 60


class SubscriptionWatchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    last_check_at: str | None = None
    last_targets: int = 0
    last_refreshed: int = 0
    last_submitted: int = 0
    last_skipped_pan: int = 0
    last_errors: list[str] = Field(default_factory=list)
    # Of the targets checked last run: still hunting a magnet that matches the
    # preferences vs. already handed to 115 offline (running or finished).
    last_hunting: int = 0
    last_queued: int = 0
    last_due: int = 0
    # Drain progress: items processed out of the due backlog of the current run.
    batch_cursor: int = 0
    batch_total: int = 0
    batch_signature: str | None = None
    last_pass_completed_at: str | None = None
    # Run in progress (or interrupted): its cutoff is reused on resume so items
    # already checked in that run are not redone.
    draining: bool = False
    drain_cutoff: str | None = None
    # Per-target last check time (pruned to current targets).
    checked_at: dict[str, str] = Field(default_factory=dict)


def targets_signature(work_ids: list[str]) -> str:
    return hashlib.sha1("\n".join(work_ids).encode("utf-8")).hexdigest()[:16]


@dataclass
class _TickStats:
    targets: int = 0
    refreshed: int = 0
    submitted: int = 0
    skipped_pan: int = 0
    hunting: int = 0
    queued: int = 0
    errors: list[str] = field(default_factory=list)


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


def due_work_ids(
    all_ids: list[str],
    checked_at: dict[str, str],
    cutoff: datetime,
    *,
    recheck_seconds: float = WATCH_RECHECK_SECONDS,
) -> list[str]:
    """Targets never checked, or last checked at least ``recheck_seconds`` before ``cutoff``."""

    threshold = cutoff - timedelta(seconds=recheck_seconds)
    due: list[str] = []
    for work_id in all_ids:
        last = _parse_iso(checked_at.get(work_id))
        if last is None or last <= threshold:
            due.append(work_id)
    return due


class SubscriptionWatchStateStore:
    def __init__(self, path: Path) -> None:
        self._path = path

    def load(self) -> SubscriptionWatchStatus:
        if not self._path.is_file():
            return SubscriptionWatchStatus()
        try:
            return SubscriptionWatchStatus.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return SubscriptionWatchStatus()

    def save(self, status: SubscriptionWatchStatus) -> SubscriptionWatchStatus:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(status.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        return status


def collect_watch_work_ids(prefs: LibraryPrefs, repo: Repository) -> list[str]:
    """want_list ∪ accepted queue ∪ enabled-subscription matches, minus dismissed."""

    dismissed: set[str] = set()
    accepted: set[str] = set()
    for item in prefs.queue:
        if not item.work_id:
            continue
        if item.status == "dismissed":
            dismissed.add(item.work_id)
        elif item.status == "accepted":
            accepted.add(item.work_id)

    targets: set[str] = set(prefs.want_list) | accepted

    by_actor: dict[str, list[Work]] = defaultdict(list)
    for actor, work in repo.list_actor_work_relations():
        by_actor[actor.name].append(work)
        by_actor[actor.id].append(work)

    for sub in prefs.subscriptions:
        if not sub.enabled:
            continue
        works = by_actor.get(sub.actor_name) or by_actor.get(sub.actor_key) or []
        payloads: list[dict[str, object]] = []
        for work in works:
            payloads.append(
                {
                    "release_date": work.release_date,
                    "actors": list(work.actors or []),
                    "title": work.title,
                    "code": work.primary_code,
                    "work_id": work.id,
                    "cast_count": len(work.actors or []),
                }
            )
        matched = filter_works_for_subscription(
            payloads, start_date=sub.effective_start_date(), max_cast=sub.max_cast
        )
        for item in matched:
            work_id = item.get("work_id")
            if isinstance(work_id, str) and work_id:
                targets.add(work_id)

    return sorted(work_id for work_id in targets if work_id not in dismissed)


def ensure_subscription_cursors(prefs: LibraryPrefs, repo: Repository) -> tuple[LibraryPrefs, bool]:
    """Initialise cursors for subscriptions that predate cursor support."""

    pending = [sub for sub in prefs.subscriptions if not sub.cursor_initialized]
    if not pending:
        return prefs, False
    by_actor: dict[str, list[Work]] = defaultdict(list)
    for actor, work in repo.list_actor_work_relations():
        by_actor[actor.name].append(work)
        by_actor[actor.id].append(work)
    updated = []
    for sub in prefs.subscriptions:
        if sub.cursor_initialized:
            updated.append(sub)
            continue
        works = by_actor.get(sub.actor_name) or by_actor.get(sub.actor_key) or []
        updated.append(initialize_subscription_cursor(sub, [work.release_date for work in works]))
    return prefs.model_copy(update={"subscriptions": updated}), True


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


class SubscriptionWatchService:
    """One tick: resolve targets, refresh magnets, optionally enqueue 115 offline."""

    def __init__(
        self,
        *,
        database: Database,
        pan: PanService,
        discover: DiscoverService,
        library_prefs_store: LibraryPrefsStore,
        state_store: SubscriptionWatchStateStore,
        task_events: TaskEventHub | None = None,
    ) -> None:
        self._database = database
        self._pan = pan
        self._discover = discover
        self._prefs = library_prefs_store
        self._state = state_store
        self._task_events = task_events
        self._lock = asyncio.Lock()

    def status(self) -> SubscriptionWatchStatus:
        cfg = self._pan.config_store.load()
        stored = self._state.load()
        return stored.model_copy(update={"enabled": bool(cfg.subscription_auto_offline)})

    async def run_once(self, *, limit: int = WATCH_BATCH_SIZE) -> SubscriptionWatchStatus:
        """Drain every target due as of this run's cutoff, ``limit`` per page."""

        async with self._lock:
            return await self._run_once_unlocked(limit=limit)

    async def _run_once_unlocked(self, *, limit: int) -> SubscriptionWatchStatus:
        cfg = self._pan.config_store.load()
        stats = _TickStats()
        started = datetime.now(UTC)
        now = started.isoformat()
        if not cfg.subscription_auto_offline:
            status = self._state.load().model_copy(update={"enabled": False, "last_check_at": now})
            return self._state.save(status)

        prefs = self._prefs.load()
        with self._database.session() as session:
            repo = Repository(session)
            prefs, changed = ensure_subscription_cursors(prefs, repo)
            all_ids = collect_watch_work_ids(prefs, repo)
        if changed:
            self._prefs.replace_subscriptions(list(prefs.subscriptions))

        previous = self._state.load()
        signature = targets_signature(all_ids)
        # Resume an interrupted drain with its original cutoff (items it already
        # checked are newer than that cutoff, so they are not redone).
        resumed_cutoff = _parse_iso(previous.drain_cutoff) if previous.draining else None
        cutoff = resumed_cutoff or started
        targets = set(all_ids)
        checked_at = {key: value for key, value in previous.checked_at.items() if key in targets}
        due = due_work_ids(all_ids, checked_at, cutoff)
        stats.targets = len(due)

        pan_status = self._pan.status()
        # "offline_ready" covers both backends (115 cid / OpenList target path).
        if "offline_ready" in pan_status:
            pan_ready = bool(pan_status.get("connected") and pan_status.get("offline_ready"))
        else:
            pan_ready = bool(pan_status.get("connected") and cfg.offline_directory_id)

        checkpoint = previous.model_copy(
            update={
                "enabled": True,
                "draining": True,
                "drain_cutoff": cutoff.isoformat(),
                "batch_cursor": 0,
                "batch_total": len(due),
                "batch_signature": signature,
                "last_due": len(due),
                "checked_at": checked_at,
            }
        )
        self._state.save(checkpoint)
        page_size = max(1, limit)
        processed = 0
        pace_next = False
        for page_start in range(0, len(due), page_size):
            page = due[page_start : page_start + page_size]
            for work_id in page:
                if pace_next:
                    # Only after an item that hit the network (magnet source /
                    # offline submit); local-only checks are not throttled.
                    await asyncio.sleep(WATCH_PACE_SECONDS)
                pace_next = True
                try:
                    touched = await self._process_work(work_id, stats=stats, pan_ready=pan_ready)
                    pace_next = touched is not False
                except Exception as exc:
                    message = f"{work_id}: {type(exc).__name__}"
                    logger.warning("subscription watch work failed: %s", message)
                    stats.errors.append(message)
                processed += 1
                checked_at[work_id] = datetime.now(UTC).isoformat()
                # Persist after every work so a restart resumes mid-drain.
                checkpoint = checkpoint.model_copy(
                    update={"batch_cursor": processed, "checked_at": dict(checked_at)}
                )
                self._state.save(checkpoint)
            if self._task_events is not None and stats.submitted:
                self._task_events.notify()

        status = SubscriptionWatchStatus(
            enabled=True,
            last_check_at=now,
            last_targets=stats.targets,
            last_refreshed=stats.refreshed,
            last_submitted=stats.submitted,
            last_skipped_pan=stats.skipped_pan,
            last_errors=stats.errors[-10:],
            last_hunting=stats.hunting,
            last_queued=stats.queued,
            last_due=len(due),
            batch_cursor=processed,
            batch_total=len(due),
            batch_signature=signature,
            last_pass_completed_at=datetime.now(UTC).isoformat(),
            draining=False,
            drain_cutoff=None,
            checked_at=checked_at,
        )
        return self._state.save(status)

    async def _sukebei_fallback(self, work_id: str, stats: _TickStats) -> list[WorkMagnet]:
        """JavDB had nothing: search Sukebei by the work's code and persist matches."""

        with self._database.session() as session:
            work = Repository(session).get_work(work_id)
            code = work.primary_code if work is not None else None
        if not code:
            return []
        try:
            fetched = await self._discover.list_magnets(provider="sukebei", external_id=code)
        except Exception as exc:
            stats.errors.append(f"{work_id}: sukebei magnets {type(exc).__name__}")
            logger.info("subscription watch sukebei failed work=%s err=%s", work_id, type(exc).__name__)
            return []
        if not fetched:
            return []
        with self._database.session() as session:
            repo = Repository(session)
            work = repo.get_work(work_id)
            if work is None:
                return []
            payload = [item.model_dump(mode="json") for item in fetched]
            created, _skipped = repo.save_work_magnets(work, payload, provider="sukebei")
            magnets = list(repo.list_work_magnets(work_id))
        if created:
            stats.refreshed += 1
        return magnets

    async def _process_work(
        self,
        work_id: str,
        *,
        stats: _TickStats,
        pan_ready: bool,
    ) -> bool:
        """Check one target; returns True when it touched the network."""

        touched = False
        with self._database.session() as session:
            repo = Repository(session)
            work = repo.get_work(work_id)
            if work is None:
                return False
            magnets = list(repo.list_work_magnets(work_id))
            lookup = _javdb_lookup(repo, work_id) if not magnets else None

        if not magnets and lookup is not None:
            touched = True
            external_id, source_url = lookup
            try:
                fetched = await self._discover.list_magnets(
                    provider="javdb",
                    external_id=external_id,
                    source_url=source_url,
                )
            except Exception as exc:
                stats.errors.append(f"{work_id}: magnet refresh {type(exc).__name__}")
                logger.info(
                    "subscription watch magnet refresh failed work=%s err=%s",
                    work_id,
                    type(exc).__name__,
                )
                fetched = ()
            if fetched:
                with self._database.session() as session:
                    repo = Repository(session)
                    work = repo.get_work(work_id)
                    if work is None:
                        return touched
                    payload = [item.model_dump(mode="json") for item in fetched]
                    created, _skipped = repo.save_work_magnets(work, payload, provider="javdb")
                    magnets = list(repo.list_work_magnets(work_id))
                if created:
                    stats.refreshed += 1

        if not magnets and self._discover.sukebei_available:
            touched = True
            sukebei_magnets = await self._sukebei_fallback(work_id, stats)
            if sukebei_magnets:
                magnets = sukebei_magnets

        if not magnets:
            stats.hunting += 1
            return touched

        best = pick_best_magnet(magnets)
        if best is None:
            # Magnets exist but none matches the preferences yet: keep hunting.
            stats.hunting += 1
            return touched

        with self._database.session() as session:
            repo = Repository(session)
            if not _work_needs_offline(repo, work_id, best):
                stats.queued += 1
                return touched

        if not pan_ready:
            stats.hunting += 1
            stats.skipped_pan += 1
            logger.info(
                "subscription watch skip offline (pan not ready) work=%s hash=%s",
                work_id,
                best.info_hash[:12],
            )
            return touched

        touched = True
        with self._database.session() as session:
            repo = Repository(session)
            try:
                result = await enqueue_work_offline(repo, self._pan, work_id, magnet_id=best.id)
            except OfflineEnqueueError as exc:
                stats.errors.append(f"{work_id}: {exc.message}")
                logger.info(
                    "subscription watch offline skipped work=%s reason=%s",
                    work_id,
                    exc.message,
                )
                if exc.status_code == 400:
                    stats.skipped_pan += 1
                stats.hunting += 1
                return touched
            stats.queued += 1
            if result.created or not result.reused_running:
                stats.submitted += 1
            logger.info(
                "subscription watch offline enqueued work=%s hash=%s created=%s",
                work_id,
                best.info_hash[:12],
                result.created,
            )
        return touched


class SubscriptionWatchPoller:
    """Periodic asyncio loop around SubscriptionWatchService."""

    def __init__(self, service: SubscriptionWatchService) -> None:
        self._service = service
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    @property
    def service(self) -> SubscriptionWatchService:
        return self._service

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shadow-mdc-subscription-watch")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=30.0)
            return
        except TimeoutError:
            pass
        while not self._stop.is_set():
            try:
                await self._service.run_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("subscription watch tick failed")
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=WATCH_IDLE_SECONDS)
                break
            except TimeoutError:
                continue
