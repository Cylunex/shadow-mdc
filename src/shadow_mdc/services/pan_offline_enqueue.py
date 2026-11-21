"""Shared offline enqueue (115 Open or OpenList backend) for the API and subscription watcher."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

import httpx

from ..db.models import PanOfflineTask, WorkMagnet, utc_now
from ..db.repository import Repository
from ..media.magnets import info_hash_from_uri, magnet_sort_key
from .offline_recovery import attempted_hashes, load_recovery, seed_recovery
from .openlist import normalize_openlist_path, offline_info_hash
from .pan import (
    PanApiError,
    PanNotConfiguredError,
    PanOfflineConflictError,
    PanOfflineExistsError,
    PanService,
)


@dataclass(frozen=True, slots=True)
class OfflineEnqueueResult:
    task: PanOfflineTask
    created: bool
    reused_running: bool = False
    reused_completed: bool = False

    @property
    def action(self) -> Literal["running", "exists", "submitted"]:
        if self.reused_running:
            return "running"
        if self.reused_completed:
            return "exists"
        return "submitted"


class OfflineEnqueueError(Exception):
    """Public, safe-to-log enqueue failure."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def resolve_offline_directory(
    pan: PanService, directory_id: str | None, *, backend: str
) -> str | None:
    """Resolve a per-task target, keeping historical default configuration compatible."""

    if directory_id is None:
        return pan.offline_target()
    cleaned = directory_id.strip()
    if backend == "openlist":
        if cleaned.startswith("/") and "\\" not in cleaned and all(ord(char) >= 32 for char in cleaned):
            try:
                path = normalize_openlist_path(cleaned)
            except ValueError as exc:
                raise OfflineEnqueueError(str(exc), status_code=422) from exc
            if path and path != "/":
                return path
        raise OfflineEnqueueError("请填写 OpenList 挂载下的绝对路径", status_code=422)
    if len(cleaned) <= 64 and cleaned.isascii() and cleaned.isdecimal():
        return cleaned
    raise OfflineEnqueueError("115 目标目录 ID 必须是数字", status_code=422)


def check_offline_directory(
    *, expected: str, backend: str, actual: str, actual_backend: str | None
) -> None:
    if backend != (actual_backend or "115_open") or expected.rstrip("/") != actual.rstrip("/"):
        provider = "OpenList" if actual_backend == "openlist" else "115"
        raise OfflineEnqueueError(
            f"已有离线任务使用 {provider} 目录 {actual}。请查看原任务", status_code=409
        )


async def enqueue_work_offline(
    repo: Repository,
    pan: PanService,
    work_id: str,
    *,
    magnet_id: str | None = None,
    url: str | None = None,
    directory_id: str | None = None,
    auto_select: bool = False,
) -> OfflineEnqueueResult:
    """Submit one magnet/url to 115 via pan reconcile and persist a local task row."""

    work = repo.get_work(work_id)
    if work is None:
        raise OfflineEnqueueError("work not found", status_code=404)

    backend = pan.backend()
    status = pan.status()
    if not status.get("connected"):
        raise OfflineEnqueueError(
            "OpenList not configured — set base URL and token in Settings"
            if backend == "openlist"
            else "115 not connected — complete QR login in Settings",
            status_code=400,
        )
    target = resolve_offline_directory(pan, directory_id, backend=backend)
    if not target:
        raise OfflineEnqueueError(
            "set OpenList offline target path first"
            if backend == "openlist"
            else "set offline directory id first",
            status_code=400,
        )

    resolved_url: str | None = None
    resolved_magnet_id: str | None = magnet_id
    info_hash_hint: str | None = None
    if magnet_id:
        magnets = {item.id: item for item in repo.list_work_magnets(work_id)}
        magnet = magnets.get(magnet_id)
        if magnet is None:
            raise OfflineEnqueueError("magnet not found", status_code=404)
        resolved_url = magnet.uri
        info_hash_hint = magnet.info_hash
    elif url is not None:
        resolved_url = url.strip()
    elif auto_select:
        magnet = pick_best_magnet(repo.list_work_magnets(work_id), expected_code=work.primary_code)
        if magnet is None:
            raise OfflineEnqueueError("这部作品没有已保存的磁力")
        resolved_url, resolved_magnet_id, info_hash_hint = magnet.uri, magnet.id, magnet.info_hash
    else:
        raise OfflineEnqueueError("magnet_id or url required", status_code=400)
    if not resolved_url:
        raise OfflineEnqueueError("empty magnet url", status_code=400)

    info_hash_hint = info_hash_hint or (
        offline_info_hash(resolved_url) if backend == "openlist" else info_hash_from_uri(resolved_url)
    )
    work_code = work.primary_code
    repo._session.commit()
    async with pan.offline_enqueue_lock(info_hash_hint or resolved_url):
        if pan.backend() != backend:
            raise OfflineEnqueueError("离线后端已切换。请重新选择目录", status_code=409)
        if info_hash_hint:
            existing = repo.find_pan_offline_by_hash(work_id, info_hash_hint)
            matching = existing
            if directory_id is not None and matching is None:
                matching = repo.find_pan_offline_task_by_info_hash(info_hash_hint)
            if matching is not None and matching.status in {"running", "done", "completed"}:
                if directory_id is not None:
                    check_offline_directory(
                        expected=target, backend=backend, actual=matching.directory_id,
                        actual_backend=matching.backend,
                    )
                if existing is not None:
                    return OfflineEnqueueResult(
                        task=existing, created=False, reused_running=existing.status == "running",
                        reused_completed=existing.status != "running",
                    )
            # Release the read transaction before waiting on the remote backend.
            repo._session.commit()
        result = await _submit_and_track(
            repo, pan, work_id, backend=backend, directory_id=target, url=resolved_url,
            magnet_id=resolved_magnet_id, info_hash_hint=info_hash_hint, work_code=work_code,
        )
        repo._session.commit()
        return result


async def _submit_and_track(
    repo: Repository, pan: PanService, work_id: str, *, backend: str, directory_id: str,
    url: str, magnet_id: str | None, info_hash_hint: str | None, work_code: str | None,
) -> OfflineEnqueueResult:
    try:
        submit_result = await pan.submit_offline_url(
            url, directory_id=directory_id, info_hash_hint=info_hash_hint,
            work_code=work_code, expected_backend=backend,
        )
    except PanNotConfiguredError as exc:
        raise OfflineEnqueueError(str(exc), status_code=400) from exc
    except PanOfflineConflictError as exc:
        raise OfflineEnqueueError(str(exc), status_code=409) from exc
    except PanOfflineExistsError as exc:
        raise OfflineEnqueueError(str(exc), status_code=409) from exc
    except PanApiError as exc:
        raise OfflineEnqueueError(str(exc), status_code=502) from exc
    except httpx.HTTPError as exc:
        raise OfflineEnqueueError(
            f"{'OpenList' if backend == 'openlist' else '115'} offline submit failed: {type(exc).__name__}",
            status_code=502,
        ) from exc

    info_hash = str(submit_result.get("info_hash") or info_hash_hint or "").upper()
    if not info_hash:
        raise OfflineEnqueueError("offline submit missing info_hash", status_code=502)
    row_backend = "openlist" if backend == "openlist" else None
    raw_task_id = submit_result.get("remote_task_id") if backend == "openlist" else None
    remote_task_id = str(raw_task_id) if raw_task_id else None

    recovery = seed_recovery(
        info_hash=info_hash, magnet_id=magnet_id, url=url
    )
    recovery.existing_result_pending = bool(submit_result.get("already_exists"))
    task_status = "done" if recovery.existing_result_pending else "running"
    task_progress = 100.0 if recovery.existing_result_pending else 0.0

    existing = repo.find_pan_offline_by_hash(work_id, info_hash)
    if existing is not None:
        if existing.status in {"running", "done", "completed"}:
            return OfflineEnqueueResult(
                task=existing, created=False, reused_running=existing.status == "running",
                reused_completed=existing.status != "running",
            )
        existing.status = task_status
        existing.progress = task_progress
        existing.error = None
        existing.directory_id = directory_id
        existing.url = url
        existing.magnet_id = magnet_id
        existing.backend = row_backend
        existing.remote_task_id = remote_task_id
        existing.file_id = None
        existing.remote_name = None
        existing.remote_path = None
        existing.strm_path = None
        existing.recovery_json = recovery.to_dict()
        existing.updated_at = utc_now()
        repo._session.flush()
        return OfflineEnqueueResult(task=existing, created=False)

    task = repo.create_pan_offline_task(
        work_id=work_id,
        info_hash=info_hash,
        directory_id=directory_id,
        url=url,
        magnet_id=magnet_id,
        backend=row_backend,
        remote_task_id=remote_task_id,
        recovery_json=recovery.to_dict(),
    )
    repo.update_pan_offline_task(task, status=task_status, progress=task_progress)
    return OfflineEnqueueResult(task=task, created=True)


def pick_best_magnet(
    magnets: list[WorkMagnet],
    *,
    expected_code: str | None = None,
) -> WorkMagnet | None:
    """Best first: code match, quality score (subtitle/resolution/no samples), then size."""

    if not magnets:
        return None
    return max(
        magnets,
        key=lambda item: magnet_sort_key(
            name=item.name,
            size_bytes=item.size_bytes,
            has_subtitle=bool(item.has_subtitle),
            hd=bool(item.hd),
            expected_code=expected_code,
        ),
    )


def pick_automatic_magnet(
    repo: Repository,
    work_id: str,
    magnets: list[WorkMagnet],
    *,
    expected_code: str | None = None,
) -> WorkMagnet | None:
    """Keep exhausted attempts terminal across watch ticks; new magnets remain eligible."""

    exhausted: set[str] = set()
    for task in repo.list_pan_offline_tasks(work_id=work_id, status="failed"):
        state = load_recovery(task)
        if state.exhausted:
            exhausted.update(attempted_hashes(state))
    return pick_best_magnet(
        [magnet for magnet in magnets if magnet.info_hash.upper() not in exhausted],
        expected_code=expected_code,
    )
