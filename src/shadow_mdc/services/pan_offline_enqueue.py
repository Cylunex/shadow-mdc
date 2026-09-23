"""Shared 115 offline enqueue used by the HTTP API and subscription watcher."""

from __future__ import annotations

from dataclasses import dataclass

import httpx

from ..db.models import PanOfflineTask, WorkMagnet, utc_now
from ..db.repository import Repository
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

    status = pan.status()
    if not status.get("connected"):
        raise OfflineEnqueueError(
            "115 not connected — complete QR login in Settings",
            status_code=400,
        )
    directory_id = pan.config_store.load().offline_directory_id
    if not directory_id:
        raise OfflineEnqueueError("set offline directory id first", status_code=400)

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

    if info_hash_hint:
        existing = repo.find_pan_offline_by_hash(work_id, info_hash_hint)
        if existing is not None and existing.status == "running":
            return OfflineEnqueueResult(task=existing, created=False, reused_running=True)

    try:
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
            f"115 offline submit failed: {type(exc).__name__}",
            status_code=502,
        ) from exc

    info_hash = str(submit_result.get("info_hash") or info_hash_hint or "").upper()
    if not info_hash:
        raise OfflineEnqueueError("115 offline submit missing info_hash", status_code=502)

    existing = repo.find_pan_offline_by_hash(work_id, info_hash)
    if existing is not None:
        existing.status = "running"
        existing.progress = 0.0
        existing.error = None
        existing.directory_id = directory_id
        existing.url = resolved_url
        existing.magnet_id = resolved_magnet_id
        existing.updated_at = utc_now()
        repo._session.flush()
        return OfflineEnqueueResult(task=existing, created=False)

    task = repo.create_pan_offline_task(
        work_id=work_id,
        info_hash=info_hash,
        directory_id=directory_id,
        url=resolved_url,
        magnet_id=resolved_magnet_id,
    )
    return OfflineEnqueueResult(task=task, created=True)


def pick_best_magnet(magnets: list[WorkMagnet]) -> WorkMagnet | None:
    """Prefer subtitle, then HD, then larger size."""

    if not magnets:
        return None
    return sorted(
        magnets,
        key=lambda item: (
            1 if item.has_subtitle else 0,
            1 if item.hd else 0,
            item.size_bytes or 0,
        ),
        reverse=True,
    )[0]
