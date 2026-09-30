"""Offline completion → STRM export (relay) → Emby notify queue; delete reconcile."""

from __future__ import annotations

import asyncio
from datetime import date
from pathlib import Path
from typing import Any

import httpx

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.media_server import MediaServerStore
from shadow_mdc.services.pan import PanApiError, PanService
from shadow_mdc.services.pan_poller import PanOfflinePoller


class _FakeClient:
    def __init__(self) -> None:
        self.gone: set[str] = set()

    async def get_task_list(self, page: int = 1) -> dict[str, Any]:
        return {
            "page_count": 1,
            "tasks": [
                {
                    "info_hash": "H" * 40,
                    "local_status": "done",
                    "progress": 100.0,
                    "file_id": "folder1",
                    "name": "ABC-123",
                }
            ],
        }

    async def get_folder_info(self, file_id: str) -> dict[str, Any]:
        if file_id in self.gone:
            raise PanApiError("gone", code=430004)
        if file_id == "folder1":
            return {
                "file_name": "ABC-123",
                "file_category": "0",
                "paths": [{"name": "根目录"}, {"name": "云下载"}],
            }
        return {"file_name": f"{file_id}.mp4", "file_category": "1", "pick_code": f"p{file_id}"}

    async def list_directory(self, directory_id: str, *, page: int = 1, limit: int = 100) -> dict[str, Any]:
        items = [
            {"id": "v1", "name": "ABC-123-CD1.mp4", "is_directory": False, "size": 100, "pick_code": "pv1"},
            {"id": "v2", "name": "ABC-123-CD2.mp4", "is_directory": False, "size": 100, "pick_code": "pv2"},
        ]
        return {"items": items, "total": 2}

    async def walk_videos(self, root_id: str, *, pacer: Any = None) -> list[Any]:
        from shadow_mdc.services.pan import Pan115Client

        async def no_sleep(_s: float) -> None:
            return None

        from shadow_mdc.services.pan import ScanPacer

        return await Pan115Client.walk_videos(self, root_id, pacer=ScanPacer(sleep=no_sleep))  # type: ignore[arg-type]


def test_poll_completion_exports_relay_and_reconcile_deletes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="abc123",
                title="ABC-123 title",
                code="ABC-123",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/abc123",
                release_date=date(2026, 9, 1),
            ),
            overwrite=True,
        )
        repo.create_pan_offline_task(work_id=work.id, info_hash="H" * 40, directory_id="d")

    pan = PanService(data_dir=data_dir)
    export_root = tmp_path / "emby"
    pan.save_settings(
        {
            "strm_enabled": True,
            "strm_output_root": str(export_root),
            "strm_mode": "relay",
            "strm_public_base_url": "http://nas:8700",
            "strm_token": "tok",
            "strm_emby_root": "/media/strm",
        }
    )
    fake = _FakeClient()
    pan.status = lambda: {"connected": True}  # type: ignore[method-assign]
    pan.get_client = lambda: fake  # type: ignore[method-assign,assignment,return-value]

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = PanOfflinePoller(
                database=database,
                pan=pan,
                media_server_store=MediaServerStore(data_dir / "media-server.json"),
                http=http,
                data_dir=data_dir,
            )
            assert await poller.poll_once() is True
            folder = export_root / "ABC-123"
            assert (
                folder / "ABC-123-cd1.strm"
            ).read_text().strip() == "http://nas:8700/api/strm/play/v1?token=tok"
            assert (
                folder / "ABC-123-cd2.strm"
            ).read_text().strip() == "http://nas:8700/api/strm/play/v2?token=tok"
            assert (folder / "ABC-123.nfo").is_file()
            assert poller.notifier.pending == {"/media/strm/ABC-123": "Created"}
            with database.session() as session:
                task = Repository(session).list_pan_offline_tasks()[0]
                assert task.status == "done"
                assert task.strm_path and task.strm_path.endswith("ABC-123-cd1.strm")

            # Token rotation rewrites in place.
            pan.save_settings({"strm_token": "tok2"})
            rewritten = await poller.rewrite()
            assert len(rewritten.rewritten) == 2
            assert (folder / "ABC-123-cd1.strm").read_text().strip().endswith("?token=tok2")

            # Source gone on 115 → folder removed + Deleted notify.
            fake.gone = {"v1", "v2"}
            import shadow_mdc.services.pan_poller as poller_module

            original = poller_module.ScanPacer

            class _NoWait(original):  # type: ignore[misc,valid-type]
                async def wait(self) -> None:
                    return None

            poller_module.ScanPacer = _NoWait  # type: ignore[misc]
            try:
                result = await poller.reconcile()
            finally:
                poller_module.ScanPacer = original  # type: ignore[misc]
            assert [path.name for path in result.removed] == ["ABC-123"]
            assert not folder.exists()
            assert poller.notifier.pending["/media/strm/ABC-123"] == "Deleted"

    asyncio.run(scenario())
