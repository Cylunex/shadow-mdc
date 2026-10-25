"""Resumable, rate-limited subtitle backfill over an existing STRM library.

The job walks every directory under a STRM root that contains ``.strm`` files,
skips directories that already hold any subtitle, and asks SubtitleCat for the
rest. Progress is stored in a :class:`JobCheckpoint` row after each directory so
a restart (or a manual stop) continues from the last finished directory.

Rate limiting is enforced by :class:`SubtitleCatFetcher` itself (a minimum gap
between HTTP requests); the job adds a per-run ``limit`` on remote lookups and
stops itself after a run of consecutive errors so an upstream outage cannot
turn into a hammering loop.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol

from pydantic import BaseModel, Field

from ..db.repository import Database, Repository
from ..enums import MediaCategory
from ..identity import extract_code
from ..media.parts import SUBTITLE_EXTENSIONS
from .subtitlecat import SubtitleFetchResult

log = logging.getLogger(__name__)

CHECKPOINT_KEY = "subtitle-backfill"
CHECKPOINT_KIND = "subtitle-backfill"
TASK_KIND = "subtitles:backfill"
MAX_CONSECUTIVE_ERRORS = 10
_REMOTE_STATUSES = frozenset({"written", "miss", "error", "dry-run"})


class SubtitleFetcherLike(Protocol):
    def fetch(
        self, code: str, directory: Path, stem: str, *, dry_run: bool = False
    ) -> SubtitleFetchResult: ...


class BackfillStats(BaseModel):
    total_dirs: int = 0
    processed: int = 0
    written: int = 0
    exists: int = 0
    miss: int = 0
    cached_miss: int = 0
    no_code: int = 0
    errors: int = 0
    dry_run: int = 0
    remote_lookups: int = 0


class BackfillCursor(BaseModel):
    root: str
    last_dir: str | None = None
    dry_run: bool = False
    limit: int | None = None


class BackfillStatus(BaseModel):
    status: str  # idle | running | paused | done | failed
    running: bool
    root: str | None = None
    last_dir: str | None = None
    task_id: str | None = None
    dry_run: bool = False
    limit: int | None = None
    stats: BackfillStats = Field(default_factory=BackfillStats)
    updated_at: str | None = None
    reason: str | None = None


def iter_strm_dirs(root: Path) -> Iterator[Path]:
    """Yield directories under ``root`` containing a ``.strm`` file, in stable order."""

    seen: set[Path] = set()
    for strm in root.rglob("*.strm"):
        if strm.is_file():
            seen.add(strm.parent)
    yield from sorted(seen, key=lambda item: str(item))


def directory_has_subtitle(directory: Path) -> bool:
    try:
        return any(
            entry.suffix.casefold() in SUBTITLE_EXTENSIONS and entry.is_file()
            for entry in directory.iterdir()
        )
    except OSError:
        return False


def primary_strm(directory: Path) -> Path | None:
    """The main-part ``.strm`` of a directory (shortest name, then alphabetical)."""

    try:
        files = [entry for entry in directory.iterdir() if entry.suffix.casefold() == ".strm"]
    except OSError:
        return None
    if not files:
        return None
    return sorted(files, key=lambda item: (len(item.stem), item.stem))[0]


def directory_code(directory: Path, strm: Path) -> str | None:
    for text in (strm.stem, directory.name):
        code, _family = extract_code(text, MediaCategory.OTHER)
        if code:
            return code
    return None


@dataclass
class SubtitleBackfillJob:
    database: Database
    fetcher_factory: Callable[[], SubtitleFetcherLike]
    dir_lister: Callable[[Path], Iterator[Path]] = iter_strm_dirs
    _stop: threading.Event = field(default_factory=threading.Event, repr=False)
    _task: asyncio.Task[None] | None = field(default=None, repr=False)
    _reason: str | None = field(default=None, repr=False)
    _shutting_down: bool = field(default=False, repr=False)

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> BackfillStatus:
        with self.database.session() as session:
            checkpoint = Repository(session).get_checkpoint(CHECKPOINT_KEY)
            if checkpoint is None:
                return BackfillStatus(status="idle", running=self.running)
            cursor = BackfillCursor.model_validate(checkpoint.cursor)
            status = checkpoint.status
            if status == "running" and not self.running:
                status = "interrupted"
            return BackfillStatus(
                status=status,
                running=self.running,
                root=cursor.root,
                last_dir=cursor.last_dir,
                task_id=checkpoint.task_id,
                dry_run=cursor.dry_run,
                limit=cursor.limit,
                stats=BackfillStats.model_validate(checkpoint.stats or {}),
                updated_at=checkpoint.updated_at.isoformat() if checkpoint.updated_at else None,
                reason=self._reason or (checkpoint.stats or {}).get("reason"),  # type: ignore[arg-type]
            )

    def start(self, root: Path, *, limit: int | None, dry_run: bool, restart: bool) -> BackfillStatus:
        if self.running:
            raise RuntimeError("subtitle backfill is already running")
        root = root.resolve()
        if not root.is_dir():
            raise ValueError(f"STRM root does not exist: {root}")
        with self.database.session() as session:
            repo = Repository(session)
            existing = repo.get_checkpoint(CHECKPOINT_KEY)
            cursor = BackfillCursor(root=str(root), dry_run=dry_run, limit=limit)
            stats = BackfillStats()
            if existing is not None and not restart and existing.status != "done":
                previous = BackfillCursor.model_validate(existing.cursor)
                if previous.root == str(root) and previous.dry_run == dry_run:
                    cursor.last_dir = previous.last_dir
                    stats = BackfillStats.model_validate(
                        {k: v for k, v in (existing.stats or {}).items() if k != "reason"}
                    )
            task = repo.create_task_run(kind=TASK_KIND, scope=str(root))
            repo.save_checkpoint(
                CHECKPOINT_KEY,
                kind=CHECKPOINT_KIND,
                status="running",
                cursor=cursor.model_dump(mode="json"),
                stats=stats.model_dump(mode="json"),
                task_id=task.id,
            )
            task_id = task.id
        self._launch(task_id)
        return self.status()

    def resume_if_running(self) -> bool:
        """Called at startup: continue a backfill that was running when the process died."""

        with self.database.session() as session:
            repo = Repository(session)
            checkpoint = repo.get_checkpoint(CHECKPOINT_KEY)
            if checkpoint is None or checkpoint.status != "running":
                return False
            cursor = BackfillCursor.model_validate(checkpoint.cursor)
            if not Path(cursor.root).is_dir():
                repo.save_checkpoint(CHECKPOINT_KEY, kind=CHECKPOINT_KIND, status="paused")
                return False
            task = repo.create_task_run(kind=TASK_KIND, scope=cursor.root)
            repo.save_checkpoint(CHECKPOINT_KEY, kind=CHECKPOINT_KIND, status="running", task_id=task.id)
            task_id = task.id
        self._launch(task_id)
        return True

    async def stop(self) -> BackfillStatus:
        self._stop.set()
        if self._task is not None:
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(asyncio.shield(self._task), timeout=60)
        return self.status()

    async def shutdown(self) -> None:
        """Stop for process exit; the checkpoint stays ``running`` so startup resumes it."""

        self._shutting_down = True
        await self.stop()

    def _launch(self, task_id: str) -> None:
        self._stop.clear()
        self._shutting_down = False
        self._reason = None
        self._task = asyncio.get_running_loop().create_task(asyncio.to_thread(self.run_sync, task_id))

    # -- worker ----------------------------------------------------------------
    def run_sync(self, task_id: str) -> str:
        """Process directories until done, stopped, or limited. Returns final status."""

        with self.database.session() as session:
            checkpoint = Repository(session).get_checkpoint(CHECKPOINT_KEY)
            if checkpoint is None:
                return "idle"
            cursor = BackfillCursor.model_validate(checkpoint.cursor)
            stats = BackfillStats.model_validate(
                {k: v for k, v in (checkpoint.stats or {}).items() if k != "reason"}
            )
        fetcher = self.fetcher_factory()
        final_status = "done"
        reason: str | None = None
        consecutive_errors = 0
        lookups_this_run = 0
        try:
            directories = list(self.dir_lister(Path(cursor.root)))
            stats.total_dirs = len(directories)
            for directory in directories:
                if cursor.last_dir is not None and str(directory) <= cursor.last_dir:
                    continue
                if self._stop.is_set():
                    if self._shutting_down:
                        final_status, reason = "running", None
                    else:
                        final_status, reason = "paused", "stopped by user"
                    break
                if self._cancelled(task_id):
                    final_status, reason = "paused", "task cancelled"
                    break
                if cursor.limit is not None and lookups_this_run >= cursor.limit:
                    final_status, reason = "paused", f"run limit {cursor.limit} reached"
                    break
                outcome = self._process(fetcher, directory, dry_run=cursor.dry_run)
                _count(stats, outcome)
                if outcome in _REMOTE_STATUSES:
                    lookups_this_run += 1
                    stats.remote_lookups += 1
                consecutive_errors = consecutive_errors + 1 if outcome == "error" else 0
                cursor.last_dir = str(directory)
                stats.processed += 1
                self._save(task_id, "running", cursor, stats)
                if consecutive_errors >= MAX_CONSECUTIVE_ERRORS:
                    final_status = "paused"
                    reason = f"{MAX_CONSECUTIVE_ERRORS} consecutive errors; upstream unhealthy"
                    break
        except Exception as exc:  # job must record failure, never crash the app
            log.exception("subtitle backfill failed")
            final_status, reason = "failed", str(exc)
        self._reason = reason
        self._finish(task_id, final_status, cursor, stats, reason)
        return final_status

    def _process(self, fetcher: SubtitleFetcherLike, directory: Path, *, dry_run: bool) -> str:
        if directory_has_subtitle(directory):
            return "exists"
        strm = primary_strm(directory)
        if strm is None:
            return "no-code"
        code = directory_code(directory, strm)
        if code is None:
            return "no-code"
        return fetcher.fetch(code, directory, strm.stem, dry_run=dry_run).status

    def _cancelled(self, task_id: str) -> bool:
        with self.database.session() as session:
            return Repository(session).is_task_cancel_requested(task_id)

    def _save(self, task_id: str, status: str, cursor: BackfillCursor, stats: BackfillStats) -> None:
        with self.database.session() as session:
            repo = Repository(session)
            repo.save_checkpoint(
                CHECKPOINT_KEY,
                kind=CHECKPOINT_KIND,
                status=status,
                cursor=cursor.model_dump(mode="json"),
                stats=stats.model_dump(mode="json"),
                task_id=task_id,
            )
            task = repo.get_task_run(task_id)
            if task is not None and task.finished_at is None:
                repo.update_task_progress(
                    task, {**stats.model_dump(mode="json"), "last_dir": cursor.last_dir}
                )

    def _finish(
        self, task_id: str, status: str, cursor: BackfillCursor, stats: BackfillStats, reason: str | None
    ) -> None:
        with self.database.session() as session:
            repo = Repository(session)
            payload = stats.model_dump(mode="json")
            if reason:
                payload["reason"] = reason
            repo.save_checkpoint(
                CHECKPOINT_KEY,
                kind=CHECKPOINT_KIND,
                status=status,
                cursor=cursor.model_dump(mode="json"),
                stats=payload,
                task_id=task_id,
            )
            task = repo.get_task_run(task_id)
            if task is not None and task.finished_at is None:
                task_status = {"done": "succeeded", "running": "interrupted"}.get(status, status)
                repo.finish_task_run(
                    task,
                    status=task_status,
                    summary={**payload, "last_dir": cursor.last_dir, "resumable": status != "done"},
                    error=reason if status == "failed" else None,
                )


def _count(stats: BackfillStats, outcome: str) -> None:
    attribute = {
        "written": "written",
        "exists": "exists",
        "miss": "miss",
        "cached-miss": "cached_miss",
        "no-code": "no_code",
        "error": "errors",
        "dry-run": "dry_run",
    }.get(outcome)
    if attribute is not None:
        setattr(stats, attribute, getattr(stats, attribute) + 1)
