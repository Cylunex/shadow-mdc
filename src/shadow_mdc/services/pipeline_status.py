"""End-to-end pan pipeline snapshot: offline → STRM → NFO → Emby.

Borrowed from TgtoDrive's "整理历史看板" idea (status visibility across stages),
implemented against OpenList + shadow-mdc — no product switch.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from sqlalchemy import func, select

from ..db.models import PanOfflineTask
from ..db.repository import Repository
from .media_server import MediaServerSettings
from .pan import PanService
from .pan_poller import PanOfflinePoller
from .strm_export import SIDECAR_NAME, iter_export_dirs, strm_config_problems
from .subscription_watch import SubscriptionWatchStatus

# Cap filesystem walks so the status endpoint stays cheap on large libraries.
_EXPORT_SAMPLE_CAP = 500


def _count_offline_by_status(repo: Repository) -> dict[str, int]:
    rows = repo._session.execute(
        select(PanOfflineTask.status, func.count()).group_by(PanOfflineTask.status)
    ).all()
    return {str(status): int(count) for status, count in rows}


def _scan_export_tree(root: Path | None) -> dict[str, Any]:
    """Summarise managed export folders (sidecar-backed) and orphan .strm files."""

    empty: dict[str, Any] = {
        "export_dirs": 0,
        "with_strm": 0,
        "with_nfo": 0,
        "missing_nfo": 0,
        "orphan_strm_files": 0,
        "sampled": False,
        "truncated": False,
    }
    if not root or not Path(root).is_dir():
        return empty
    path = Path(root)
    export_dirs = 0
    with_strm = 0
    with_nfo = 0
    missing_nfo = 0
    orphan_strm = 0
    managed_parents: set[Path] = set()
    truncated = False
    for directory in iter_export_dirs(path):
        export_dirs += 1
        managed_parents.add(directory)
        has_strm = any(directory.glob("*.strm"))
        has_nfo = (directory / "movie.nfo").is_file() or any(directory.glob("*.nfo"))
        if has_strm:
            with_strm += 1
        if has_nfo:
            with_nfo += 1
        elif has_strm:
            missing_nfo += 1
        if export_dirs >= _EXPORT_SAMPLE_CAP:
            truncated = True
            break
    # Orphan .strm = file whose parent has no sidecar (not managed by shadow-mdc).
    scanned_strm = 0
    for strm in path.rglob("*.strm"):
        scanned_strm += 1
        if scanned_strm > _EXPORT_SAMPLE_CAP * 4:
            truncated = True
            break
        if strm.parent in managed_parents:
            continue
        if (strm.parent / SIDECAR_NAME).is_file():
            continue
        orphan_strm += 1
    return {
        "export_dirs": export_dirs,
        "with_strm": with_strm,
        "with_nfo": with_nfo,
        "missing_nfo": missing_nfo,
        "orphan_strm_files": orphan_strm,
        "sampled": True,
        "truncated": truncated,
    }


def build_pipeline_status(
    *,
    pan: PanService,
    poller: PanOfflinePoller,
    repo: Repository,
    media_settings: MediaServerSettings,
    subscription_status: SubscriptionWatchStatus | None = None,
) -> dict[str, Any]:
    """Aggregate offline → strm → nfo → emby into one UI/API-friendly snapshot."""

    cfg = pan.config_store.load()
    pan_status = pan.status()
    offline_counts = _count_offline_by_status(repo)
    recent = [
        {
            "id": row.id,
            "work_id": row.work_id,
            "status": row.status,
            "progress": row.progress,
            "remote_name": row.remote_name,
            "strm_path": row.strm_path,
            "error": row.error,
            "backend": row.backend,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }
        for row in repo.list_pan_offline_tasks(limit=12)
    ]
    export = _scan_export_tree(cfg.strm_output_root if cfg.strm_enabled else None)
    config_errors = strm_config_problems(cfg, emby_notify=bool(media_settings.enabled))
    maintenance = poller.maintenance
    notifier = poller.notifier
    last = notifier.last_result
    emby_ready = bool(media_settings.enabled and media_settings.base_url and media_settings.api_key)
    emby_needs = []
    if not media_settings.enabled:
        emby_needs.append("media_server.enabled is false (notify queue stays idle)")
    if media_settings.enabled and not media_settings.base_url:
        emby_needs.append("media_server.base_url missing")
    if media_settings.enabled and not media_settings.api_key:
        emby_needs.append("media_server.api_key missing (Emby credentials)")
    if media_settings.enabled and not (cfg.strm_emby_root or "").strip():
        emby_needs.append("strm_emby_root missing (path Emby sees)")

    playback_notes: list[str] = []
    if cfg.strm_mode == "openlist" and cfg.pan_backend == "openlist":
        playback_notes.append(
            "STRM bodies point at OpenList /d{path}[?sign=]; Emby clients follow 302 to 115 CDN."
        )
        playback_notes.append(
            "Prefer Emby → OpenList public URL (or shadow-mdc /api/strm/openlist relay) so video "
            "bytes never traverse the NAS uplink."
        )
    elif cfg.strm_mode == "relay":
        playback_notes.append(
            "STRM bodies hit /api/strm/play/{file_id} (or /api/strm/openlist/…) which 302s to a "
            "fresh CDN URL; keep strm_public_base_url reachable from Emby clients."
        )
    if cfg.openlist_strm_sign:
        playback_notes.append("OpenList sign= is enabled; relay refreshes signs on each play.")

    stages = {
        "offline": {
            "backend": pan_status.get("backend") or cfg.pan_backend,
            "connected": bool(pan_status.get("connected")),
            "offline_ready": bool(pan_status.get("offline_ready")),
            "target": pan_status.get("offline_target"),
            "counts": {
                "running": offline_counts.get("running", 0),
                "done": offline_counts.get("done", 0),
                "failed": offline_counts.get("failed", 0),
                "other": sum(
                    count
                    for status, count in offline_counts.items()
                    if status not in {"running", "done", "failed"}
                ),
            },
            "auto_export_on_complete": True,
            "recent": recent,
        },
        "strm": {
            "enabled": cfg.strm_enabled,
            "mode": cfg.strm_mode,
            "output_root": cfg.strm_output_root,
            "layout_template": cfg.strm_layout_template,
            "config_errors": config_errors,
            "export_dirs": export["export_dirs"],
            "with_strm": export["with_strm"],
            "orphan_strm_files": export["orphan_strm_files"],
            "truncated": export["truncated"],
            "maintenance_running": maintenance.running,
            "last_reconcile_at": maintenance.last_reconcile_at,
            "last_reconcile": maintenance.last_reconcile,
            "last_rewrite_at": maintenance.last_rewrite_at,
            "last_rematerialize_at": maintenance.last_rematerialize_at,
        },
        "nfo": {
            "with_nfo": export["with_nfo"],
            "missing_nfo": export["missing_nfo"],
            "note": "movie.nfo is written beside .strm on offline completion when Work metadata exists",
        },
        "emby": {
            "notify_enabled": bool(media_settings.enabled),
            "ready": emby_ready,
            "kind": media_settings.kind,
            "base_url_set": bool(media_settings.base_url),
            "api_key_set": bool(media_settings.api_key),
            "strm_emby_root": cfg.strm_emby_root,
            "pending": len(notifier.pending),
            "needs": emby_needs,
            "last": None
            if last is None
            else {
                "attempted": last.attempted,
                "ok": last.ok,
                "sent": last.sent,
                "detail": last.detail,
            },
        },
    }

    return {
        "backend": pan_status.get("backend") or cfg.pan_backend,
        "connected": bool(pan_status.get("connected")),
        "reason": pan_status.get("reason"),
        "stages": stages,
        "playback": {
            "mode": cfg.strm_mode,
            "notes": playback_notes,
        },
        "subscription_watch": None
        if subscription_status is None
        else subscription_status.model_dump(),
        "flow": [
            "intake/magnet → OpenList|115 offline",
            "poller detects done → walk videos",
            "export {studio}/{CODE}/ (.strm + movie.nfo + artwork)",
            "Emby Library/Media/Updated queue (when media_server.enabled)",
        ],
    }
