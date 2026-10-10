"""Shared offline enqueue (115 Open or OpenList backend) for the API and subscription watcher."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..db.models import PanOfflineTask, WorkMagnet, utc_now
from ..db.repository import Repository
from ..media.magnets import magnet_sort_key
from .offline_recovery import seed_recovery
from .openlist import offline_info_hash
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


class OfflineEnqueueError(Exception):
    """Public, safe-to-log enqueue failure."""

    def __init__(self, message: str, *, status_code: int = 400) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


async def enqueue_work_offline(
    repo: Repository,
    pan: PanService,
    work_id: str,
    *,
    magnet_id: str | None = None,
    url: str | None = None,
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
    directory_id = pan.offline_target()
    if not directory_id:
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
    elif url:
        resolved_url = url.strip()
    else:
        raise OfflineEnqueueError("magnet_id or url required", status_code=400)
    if not resolved_url:
        raise OfflineEnqueueError("empty magnet url", status_code=400)

    if backend == "openlist" and not info_hash_hint:
        info_hash_hint = offline_info_hash(resolved_url)
    if info_hash_hint:
        existing = repo.find_pan_offline_by_hash(work_id, info_hash_hint)
        if existing is not None and existing.status == "running":
            return OfflineEnqueueResult(task=existing, created=False, reused_running=True)

    # End the read transaction before the slow network submit so no SQLite
    # snapshot is pinned across it (the row write below opens a fresh one);
    # the subscription watcher and scrape/enrich writers never wait on 115.
    repo._session.commit()
    try:
        if backend == "openlist":
            submit_result = await pan.submit_offline_url(
                resolved_url,
                directory_id=directory_id,
                info_hash_hint=info_hash_hint,
                work_code=work.primary_code,
            )
        else:
            submit_result = await pan.submit_offline_url(
                resolved_url,
                directory_id=directory_id,
                info_hash_hint=info_hash_hint,
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
        raise OfflineEnqueueError("115 offline submit missing info_hash", status_code=502)
    row_backend = "openlist" if backend == "openlist" else None
    raw_task_id = submit_result.get("remote_task_id") if backend == "openlist" else None
    remote_task_id = str(raw_task_id) if raw_task_id else None

    recovery = seed_recovery(
        info_hash=info_hash, magnet_id=resolved_magnet_id, url=resolved_url
    ).to_dict()

    existing = repo.find_pan_offline_by_hash(work_id, info_hash)
    if existing is not None:
        existing.status = "running"
        existing.progress = 0.0
        existing.error = None
        existing.directory_id = directory_id
        existing.url = resolved_url
        existing.magnet_id = resolved_magnet_id
        existing.backend = row_backend
        existing.remote_task_id = remote_task_id
        existing.recovery_json = recovery
        existing.updated_at = utc_now()
        repo._session.flush()
        return OfflineEnqueueResult(task=existing, created=False)

    task = repo.create_pan_offline_task(
        work_id=work_id,
        info_hash=info_hash,
        directory_id=directory_id,
        url=resolved_url,
        magnet_id=resolved_magnet_id,
        backend=row_backend,
        remote_task_id=remote_task_id,
        recovery_json=recovery,
    )
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
