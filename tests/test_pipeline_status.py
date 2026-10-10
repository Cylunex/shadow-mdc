"""Pipeline status snapshot (offline → strm → nfo → emby)."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.pipeline_status import build_pipeline_status
from shadow_mdc.services.strm_export import export_work
from shadow_mdc.services.pan import PanSettings, RemoteVideo


def _video(fid: str, name: str) -> RemoteVideo:
    return RemoteVideo(file_id=fid, name=name, pick_code=f"pc{fid}", relative_path=name, size=1000)


def test_build_pipeline_status_counts_export_and_emby_needs(tmp_path: Path) -> None:
    root = tmp_path / "strm"
    settings = PanSettings(
        strm_enabled=True,
        strm_output_root=str(root),
        strm_mode="relay",
        strm_public_base_url="http://nas.lan:8700",
        pan_backend="openlist",
        openlist_base_url="http://ol:5244",
        openlist_offline_path="/115/dl",
    )
    export_work(settings=settings, code="PIPE-1", videos=[_video("1", "a.mp4")])

    pan = MagicMock()
    pan.config_store.load.return_value = settings
    pan.status.return_value = {
        "backend": "openlist",
        "connected": True,
        "offline_ready": True,
        "offline_target": "/115/dl",
        "reason": "ok",
    }

    poller = MagicMock()
    poller.maintenance = SimpleNamespace(
        running=None,
        last_reconcile_at=None,
        last_reconcile={},
        last_rewrite_at=None,
        last_rematerialize_at=None,
    )
    poller.notifier = SimpleNamespace(pending={}, last_result=None)

    repo = MagicMock()
    repo._session = MagicMock()
    repo._session.execute.return_value.all.return_value = [("running", 2), ("done", 5), ("failed", 1)]
    repo.list_pan_offline_tasks.return_value = []

    media = MediaServerSettings(enabled=True, kind="emby", base_url=None, api_key=None)
    snap = build_pipeline_status(
        pan=pan, poller=poller, repo=repo, media_settings=media, subscription_status=None
    )
    assert snap["stages"]["offline"]["counts"]["running"] == 2
    assert snap["stages"]["offline"]["counts"]["done"] == 5
    assert snap["stages"]["strm"]["export_dirs"] == 1
    assert snap["stages"]["strm"]["with_strm"] == 1
    assert snap["stages"]["emby"]["notify_enabled"] is True
    assert snap["stages"]["emby"]["ready"] is False
    assert any("api_key" in item for item in snap["stages"]["emby"]["needs"])
    assert snap["stages"]["offline"]["auto_export_on_complete"] is True
