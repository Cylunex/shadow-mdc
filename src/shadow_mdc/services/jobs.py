"""Background TaskRun runner with cancel/retry and progress updates."""

from __future__ import annotations

import asyncio
import logging
import traceback
from collections.abc import Awaitable, Callable, Coroutine
from dataclasses import dataclass, field
from typing import Any

from ..db.repository import Database, Repository

logger = logging.getLogger(__name__)

JobBody = Callable[[Repository, Callable[[dict[str, object]], None], Callable[[], bool]], Awaitable[dict[str, object]]]


@dataclass
class _TrackedJob:
    task_id: str
    kind: str
    scope: str
    asyncio_task: asyncio.Task[None] | None = None
    retry_payload: dict[str, Any] = field(default_factory=dict)


class JobManager:
    """In-process durable-ish jobs backed by TaskRun rows."""

    def __init__(self, database: Database):
        self._database = database
        self._jobs: dict[str, _TrackedJob] = {}
        self._lock = asyncio.Lock()

    async def start(
        self,
        *,
        kind: str,
        scope: str,
        body: JobBody,
        retry_payload: dict[str, Any] | None = None,
    ) -> str:
        with self._database.session() as session:
            repo = Repository(session)
            task = repo.create_task_run(kind=kind, scope=scope)
            repo.update_task_progress(
                task,
                {"phase": "queued", "progress_current": 0, "progress_total": 0},
            )
            task_id = task.id

        tracked = _TrackedJob(
            task_id=task_id,
            kind=kind,
            scope=scope,
            retry_payload=dict(retry_payload or {}),
        )
        async with self._lock:
            self._jobs[task_id] = tracked

        async def runner() -> None:
            try:
                await self._run_body(task_id, body)
            except asyncio.CancelledError:
                self._finish(task_id, status="cancelled", summary={"phase": "cancelled"}, error=None)
                raise
            except Exception as exc:  # noqa: BLE001 - surface to TaskRun
                logger.exception("job %s failed", task_id)
                self._finish(
                    task_id,
                    status="failed",
                    summary={"phase": "failed"},
                    error=f"{type(exc).__name__}: {exc}",
                )

        tracked.asyncio_task = asyncio.create_task(runner(), name=f"shadow-mdc-job-{task_id}")
        return task_id

    async def _run_body(self, task_id: str, body: JobBody) -> None:
        def progress(summary: dict[str, object]) -> None:
            with self._database.session() as session:
                repo = Repository(session)
                task = repo.get_task_run(task_id)
                if task is None:
                    return
                repo.update_task_progress(task, summary)

        def cancelled() -> bool:
            with self._database.session() as session:
                return Repository(session).is_task_cancel_requested(task_id)

        progress({"phase": "running"})
        with self._database.session() as session:
            repo = Repository(session)
            summary = await body(repo, progress, cancelled)
            if cancelled():
                repo.finish_task_run(repo.get_task_run(task_id), status="cancelled", summary=summary)  # type: ignore[arg-type]
            else:
                status = str(summary.get("status") or "succeeded")
                if status not in {"succeeded", "partial", "failed", "cancelled"}:
                    status = "succeeded"
                task = repo.get_task_run(task_id)
                assert task is not None
                repo.finish_task_run(task, status=status, summary=summary)

    def _finish(
        self,
        task_id: str,
        *,
        status: str,
        summary: dict[str, object],
        error: str | None,
    ) -> None:
        with self._database.session() as session:
            repo = Repository(session)
            task = repo.get_task_run(task_id)
            if task is None or task.finished_at is not None:
                return
            merged = dict(task.summary or {})
            merged.update(summary)
            repo.finish_task_run(task, status=status, summary=merged, error=error)

    def request_cancel(self, task_id: str) -> None:
        with self._database.session() as session:
            repo = Repository(session)
            task = repo.get_task_run(task_id)
            if task is None:
                raise LookupError("task not found")
            repo.request_task_cancel(task)
        tracked = self._jobs.get(task_id)
        if tracked and tracked.asyncio_task and not tracked.asyncio_task.done():
            # Cooperative cancel via flag; hard-cancel only if still queued.
            pass

    async def retry(self, task_id: str, factory: Callable[[dict[str, Any]], Coroutine[Any, Any, str]]) -> str:
        tracked = self._jobs.get(task_id)
        payload = dict(tracked.retry_payload) if tracked else {}
        with self._database.session() as session:
            task = Repository(session).get_task_run(task_id)
            if task is None:
                raise LookupError("task not found")
            if task.finished_at is None:
                raise ValueError("task is still running")
            payload.setdefault("kind", task.kind)
            payload.setdefault("scope", task.scope)
        return await factory(payload)
