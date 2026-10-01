"""Background watcher: subscribed/want works → refresh magnets → optional offline (115 or OpenList)."""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import logging
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, datetime
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
WATCH_BATCH_SIZE = 5
WATCH_PACE_SECONDS = 2.0


class SubscriptionWatchStatus(BaseModel):
    model_config = ConfigDict(extra="forbid")

    enabled: bool = True
    last_check_at: str | None = None
    last_targets: int = 0
    last_refreshed: int = 0
    last_submitted: int = 0
    last_skipped_pan: int = 0
    last_errors: list[str] = Field(default_factory=list)
    # Resume checkpoint: index into the sorted target list processed so far.
    batch_cursor: int = 0
    batch_total: int = 0
    batch_signature: str | None = None
    last_pass_completed_at: str | None = None


def targets_signature(work_ids: list[str]) -> str:
    return hashlib.sha1("\n".join(work_ids).encode("utf-8")).hexdigest()[:16]


@dataclass
class _TickStats:
    targets: int = 0
    refreshed: int = 0
    submitted: int = 0
    skipped_pan: int = 0
    errors: list[str] = field(default_factory=list)


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
        async with self._lock:
            return await self._run_once_unlocked(limit=limit)

    async def _run_once_unlocked(self, *, limit: int) -> SubscriptionWatchStatus:
        cfg = self._pan.config_store.load()
        stats = _TickStats()
        now = datetime.now(UTC).isoformat()
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

        # Resume from the persisted processed-count checkpoint instead of always
        # restarting at the head of the list (which starved later targets and
        # re-did work after a restart). Wrap to 0 after a full pass.
        previous = self._state.load()
        signature = targets_signature(all_ids)
        start = previous.batch_cursor if 0 <= previous.batch_cursor < len(all_ids) else 0
        work_ids = all_ids[start : start + max(limit, 0)]
        stats.targets = len(work_ids)

        pan_status = self._pan.status()
        # "offline_ready" covers both backends (115 cid / OpenList target path).
        if "offline_ready" in pan_status:
            pan_ready = bool(pan_status.get("connected") and pan_status.get("offline_ready"))
        else:
            pan_ready = bool(pan_status.get("connected") and cfg.offline_directory_id)

        checkpoint = previous.model_copy(
            update={
                "enabled": True,
                "batch_cursor": start,
                "batch_total": len(all_ids),
                "batch_signature": signature,
            }
        )
        for index, work_id in enumerate(work_ids):
            if index > 0:
                await asyncio.sleep(WATCH_PACE_SECONDS)
            try:
                await self._process_work(work_id, stats=stats, pan_ready=pan_ready)
            except Exception as exc:
                message = f"{work_id}: {type(exc).__name__}"
                logger.warning("subscription watch work failed: %s", message)
                stats.errors.append(message)
            # Persist after every work so a restart resumes mid-batch.
            checkpoint = checkpoint.model_copy(update={"batch_cursor": start + index + 1})
            self._state.save(checkpoint)

        if self._task_events is not None and stats.submitted:
            self._task_events.notify()

        next_cursor = start + len(work_ids)
        completed_at = previous.last_pass_completed_at
        if next_cursor >= len(all_ids):
            next_cursor = 0
            completed_at = now
        status = SubscriptionWatchStatus(
            enabled=True,
            last_check_at=now,
            last_targets=stats.targets,
            last_refreshed=stats.refreshed,
            last_submitted=stats.submitted,
            last_skipped_pan=stats.skipped_pan,
            last_errors=stats.errors[-10:],
            batch_cursor=next_cursor,
            batch_total=len(all_ids),
            batch_signature=signature,
            last_pass_completed_at=completed_at,
        )
        return self._state.save(status)

    async def _process_work(
        self,
        work_id: str,
        *,
        stats: _TickStats,
        pan_ready: bool,
    ) -> None:
        with self._database.session() as session:
            repo = Repository(session)
            work = repo.get_work(work_id)
            if work is None:
                return
            magnets = list(repo.list_work_magnets(work_id))
            lookup = _javdb_lookup(repo, work_id) if not magnets else None

        if not magnets and lookup is not None:
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
                        return
                    payload = [item.model_dump(mode="json") for item in fetched]
                    created, _skipped = repo.save_work_magnets(work, payload, provider="javdb")
                    magnets = list(repo.list_work_magnets(work_id))
                if created:
                    stats.refreshed += 1

        if not magnets:
            return

        best = pick_best_magnet(magnets)
        if best is None:
            return

        with self._database.session() as session:
            repo = Repository(session)
            if not _work_needs_offline(repo, work_id, best):
                return

        if not pan_ready:
            stats.skipped_pan += 1
            logger.info(
                "subscription watch skip offline (pan not ready) work=%s hash=%s",
                work_id,
                best.info_hash[:12],
            )
            return

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
                return
            if result.created or not result.reused_running:
                stats.submitted += 1
            logger.info(
                "subscription watch offline enqueued work=%s hash=%s created=%s",
                work_id,
                best.info_hash[:12],
                result.created,
            )


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
