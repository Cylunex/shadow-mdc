"""Cancel / try-next / automatic magnet failover for pan offline tasks."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

import httpx

from ..db.models import PanOfflineTask
from ..db.repository import Database, Repository
from .offline_recovery import (
    SWITCH_REASON_MANUAL,
    after_removal_for_switch,
    after_submit,
    begin_cancel,
    begin_switch,
    detect_stall_or_fail,
    load_recovery,
    mark_removal_started,
    next_magnet_candidate,
    observe_progress,
    project_offline,
)
from .pan import (
    PanApiError,
    PanNotConfiguredError,
    PanOfflineConflictError,
    PanOfflineExistsError,
    PanService,
)

logger = logging.getLogger(__name__)


class OfflineControlError(Exception):
    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


@dataclass(frozen=True, slots=True)
class OfflineControlResult:
    task: PanOfflineTask
    changed: bool


class OfflineRecoveryController:
    """Observe running offline rows and advance at most one remote action per call."""

    def __init__(self, *, database: Database, pan: PanService) -> None:
        self._database = database
        self._pan = pan

    def auto_switch_enabled(self) -> bool:
        return bool(self._pan.config_store.load().auto_switch)

    def _busy_hashes(self, repo: Repository, work_id: str, *, except_task_id: str) -> set[str]:
        """Hashes already running/done for this work (do not failover onto them)."""

        busy: set[str] = set()
        for row in repo.list_pan_offline_tasks(work_id=work_id, limit=100):
            if row.id == except_task_id:
                continue
            if row.status in {"running", "done"} and row.info_hash:
                busy.add(row.info_hash.upper())
        return busy

    def project(self, task: PanOfflineTask) -> dict[str, Any]:
        projection = project_offline(task)
        return {
            "download_state": projection.download_state,
            "attempt_count": projection.attempt_count,
            "switch_reason": projection.switch_reason,
            "can_cancel": projection.can_cancel,
            "can_switch": projection.can_switch,
        }

    async def request_cancel(self, task_id: str) -> OfflineControlResult:
        with self._database.session() as session:
            repo = Repository(session)
            task = repo.get_pan_offline_task(task_id)
            if task is None:
                raise OfflineControlError("offline task not found", status_code=404)
            if task.status not in {"running", "failed"}:
                raise OfflineControlError("task cannot be cancelled", status_code=409)
            state = begin_cancel(load_recovery(task), manual=True)
            repo.update_pan_offline_task(task, recovery_json=state.to_dict())
            task_id = task.id
        return await self.advance(task_id)

    async def request_next(self, task_id: str) -> OfflineControlResult:
        with self._database.session() as session:
            repo = Repository(session)
            task = repo.get_pan_offline_task(task_id)
            if task is None:
                raise OfflineControlError("offline task not found", status_code=404)
            if task.status not in {"running", "failed"}:
                raise OfflineControlError("task cannot switch magnets", status_code=409)
            state = load_recovery(task)
            if state.action:
                raise OfflineControlError("recovery action already in progress", status_code=409)
            magnets = list(repo.list_work_magnets(task.work_id))
            work = repo.get_work(task.work_id)
            candidate = next_magnet_candidate(
                magnets,
                state,
                exclude_hashes=self._busy_hashes(repo, task.work_id, except_task_id=task.id),
                expected_code=work.primary_code if work is not None else None,
            )
            state = begin_switch(
                state, reason=SWITCH_REASON_MANUAL, next_magnet=candidate, manual=True
            )
            if state.exhausted:
                repo.update_pan_offline_task(
                    task,
                    status="failed",
                    error="no alternate magnets left to try",
                    recovery_json=state.to_dict(),
                )
                return OfflineControlResult(task=task, changed=True)
            repo.update_pan_offline_task(task, recovery_json=state.to_dict())
            task_id = task.id
        return await self.advance(task_id)

    def note_observation(
        self,
        repo: Repository,
        task: PanOfflineTask,
        *,
        progress: float,
        remote_failed: bool,
        remote_running: bool,
    ) -> bool:
        """Update checkpoint from a poll tick; queue a switch when stall/fail fires.

        Returns True when recovery_json was mutated.
        """

        state = load_recovery(task)
        if state.action or state.exhausted:
            if abs(float(progress) - state.progress) >= 0.05 or state.observed_at is None:
                observe_progress(state, progress=progress)
                repo.update_pan_offline_task(task, recovery_json=state.to_dict())
                return True
            return False
        observe_progress(state, progress=progress)
        reason = detect_stall_or_fail(
            state,
            remote_failed=remote_failed,
            remote_running=remote_running,
            auto_switch=self.auto_switch_enabled(),
        )
        if reason is None:
            repo.update_pan_offline_task(task, recovery_json=state.to_dict())
            return True
        magnets = list(repo.list_work_magnets(task.work_id))
        work = repo.get_work(task.work_id)
        candidate = next_magnet_candidate(
            magnets,
            state,
            exclude_hashes=self._busy_hashes(repo, task.work_id, except_task_id=task.id),
            expected_code=work.primary_code if work is not None else None,
        )
        state = begin_switch(state, reason=reason, next_magnet=candidate, manual=False)
        if state.exhausted:
            # Preserve the poller's remote failure text when no alternate remains.
            existing = (task.error or "").strip()
            if existing and not existing.startswith("magnet failover"):
                message = existing
            else:
                message = f"magnet failover exhausted ({reason})"
            repo.update_pan_offline_task(
                task,
                status="failed",
                error=message[:500],
                progress=progress,
                recovery_json=state.to_dict(),
            )
        else:
            # Keep the remote failure/stall error text; switch_reason is on recovery.
            repo.update_pan_offline_task(
                task,
                progress=progress,
                recovery_json=state.to_dict(),
            )
        return True

    async def advance_pending(self) -> int:
        """Advance one queued recovery action (cancel/switch/submit). Returns count."""

        with self._database.session() as session:
            repo = Repository(session)
            pending_ids: list[str] = []
            for row in repo.list_pan_offline_tasks(limit=200):
                if row.status not in {"running", "failed"}:
                    continue
                state = load_recovery(row)
                if state.action:
                    pending_ids.append(row.id)
        advanced = 0
        for task_id in pending_ids[:1]:  # at most one remote action per poll wave
            try:
                result = await self.advance(task_id)
            except Exception:
                logger.exception("offline recovery advance failed for %s", task_id)
                continue
            if result.changed:
                advanced += 1
        return advanced

    async def advance(self, task_id: str) -> OfflineControlResult:
        with self._database.session() as session:
            repo = Repository(session)
            task = repo.get_pan_offline_task(task_id)
            if task is None:
                raise OfflineControlError("offline task not found", status_code=404)
            state = load_recovery(task)
            action = state.action
            backend = task.backend or "115_open"
            remote_task_id = task.remote_task_id
            info_hash = task.info_hash
            directory_id = task.directory_id
            work_id = task.work_id
            next_hash = state.next_hash
            next_url = state.next_url
            next_magnet_id = state.next_magnet_id
            work = repo.get_work(work_id)
            work_code = work.primary_code if work is not None else None
            if action and not state.removal_started and action in {"cancel", "switch"}:
                state = mark_removal_started(state)
                repo.update_pan_offline_task(task, recovery_json=state.to_dict())

        if action == "":
            with self._database.session() as session:
                task = Repository(session).get_pan_offline_task(task_id)
                assert task is not None
                return OfflineControlResult(task=task, changed=False)

        if action in {"cancel", "switch"}:
            await self._remove_remote(
                backend=backend,
                info_hash=info_hash,
                remote_task_id=remote_task_id,
            )
            with self._database.session() as session:
                repo = Repository(session)
                task = repo.get_pan_offline_task(task_id)
                if task is None:
                    raise OfflineControlError("offline task not found", status_code=404)
                state = load_recovery(task)
                if action == "cancel":
                    state.action = ""
                    state.removal_started = False
                    repo.update_pan_offline_task(
                        task,
                        status="cancelled",
                        error="cancelled by user",
                        remote_task_id=None,
                        recovery_json=state.to_dict(),
                    )
                    return OfflineControlResult(task=task, changed=True)
                state = after_removal_for_switch(state)
                repo.update_pan_offline_task(
                    task,
                    remote_task_id=None,
                    recovery_json=state.to_dict(),
                )
            return await self.advance(task_id)

        if action == "submit":
            if not next_url or not next_hash:
                with self._database.session() as session:
                    repo = Repository(session)
                    task = repo.get_pan_offline_task(task_id)
                    assert task is not None
                    state = load_recovery(task)
                    state.exhausted = True
                    state.action = ""
                    repo.update_pan_offline_task(
                        task,
                        status="failed",
                        error="magnet failover missing next candidate",
                        recovery_json=state.to_dict(),
                    )
                    return OfflineControlResult(task=task, changed=True)
            try:
                submit_result = await self._pan.submit_offline_url(
                    next_url,
                    directory_id=directory_id,
                    info_hash_hint=next_hash,
                    work_code=work_code,
                )
            except (
                PanNotConfiguredError,
                PanOfflineConflictError,
                PanOfflineExistsError,
                PanApiError,
            ) as exc:
                with self._database.session() as session:
                    repo = Repository(session)
                    task = repo.get_pan_offline_task(task_id)
                    assert task is not None
                    state = load_recovery(task)
                    state.action = ""
                    state.reason = f"submit_failed:{type(exc).__name__}"
                    repo.update_pan_offline_task(
                        task,
                        status="failed",
                        error=f"failover submit failed: {exc}"[:500],
                        recovery_json=state.to_dict(),
                    )
                    return OfflineControlResult(task=task, changed=True)
            except httpx.HTTPError as exc:
                with self._database.session() as session:
                    repo = Repository(session)
                    task = repo.get_pan_offline_task(task_id)
                    assert task is not None
                    state = load_recovery(task)
                    state.action = ""
                    repo.update_pan_offline_task(
                        task,
                        status="failed",
                        error=f"failover submit failed: {type(exc).__name__}",
                        recovery_json=state.to_dict(),
                    )
                    return OfflineControlResult(task=task, changed=True)

            info_hash = str(submit_result.get("info_hash") or next_hash).upper()
            raw_tid = submit_result.get("remote_task_id")
            remote_task_id = str(raw_tid) if raw_tid else None
            with self._database.session() as session:
                repo = Repository(session)
                task = repo.get_pan_offline_task(task_id)
                assert task is not None
                state = load_recovery(task)
                state = after_submit(
                    state,
                    info_hash=info_hash,
                    magnet_id=next_magnet_id,
                    url=next_url,
                )
                updates: dict[str, object] = {
                    "status": "running",
                    "progress": 0.0,
                    "error": None,
                    "info_hash": info_hash,
                    "url": next_url,
                    "remote_task_id": remote_task_id,
                    "recovery_json": state.to_dict(),
                }
                if next_magnet_id is not None:
                    updates["magnet_id"] = next_magnet_id
                repo.update_pan_offline_task(task, **updates)  # type: ignore[arg-type]
                return OfflineControlResult(task=task, changed=True)

        with self._database.session() as session:
            task = Repository(session).get_pan_offline_task(task_id)
            assert task is not None
            return OfflineControlResult(task=task, changed=False)

    async def _remove_remote(
        self,
        *,
        backend: str,
        info_hash: str,
        remote_task_id: str | None,
    ) -> None:
        """Delete the remote offline task before any replacement magnet is submitted."""

        if backend == "openlist":
            if remote_task_id:
                await self._pan.openlist.cancel_offline_task(remote_task_id)
            return
        # 115 Open: clear offline history only (never cloud files).
        if info_hash:
            client = self._pan.get_client()
            await client.remove_offline_history(info_hash)
