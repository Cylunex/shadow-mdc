"""Background poller for 115 offline tasks → STRM + Emby refresh."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ..db.repository import Database, Repository
from .media_server import MediaServerConnector, MediaServerStore
from .pan import PanService, write_offline_strm
from .task_events import TaskEventHub

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

POLL_IDLE_SECONDS = 8.0
POLL_ACTIVE_SECONDS = 3.0
_ROOT_NAMES = frozenset({"根目录", "根目錄", "/"})


class PanOfflinePoller:
    def __init__(
        self,
        *,
        database: Database,
        pan: PanService,
        media_server_store: MediaServerStore,
        http: httpx.AsyncClient,
        task_events: TaskEventHub | None = None,
    ):
        self._database = database
        self._pan = pan
        self._media_server_store = media_server_store
        self._http = http
        self._task_events = task_events
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        self._task = asyncio.create_task(self._loop(), name="shadow-mdc-pan-offline-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None

    async def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                had_running = await self.poll_once()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("115 offline poller iteration failed")
                had_running = False
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=POLL_ACTIVE_SECONDS if had_running else POLL_IDLE_SECONDS,
                )
                break
            except TimeoutError:
                continue

    async def poll_once(self) -> bool:
        with self._database.session() as session:
            repo = Repository(session)
            running = repo.list_running_pan_offline_tasks()
            if not running:
                return False

        status = self._pan.status()
        if not status.get("connected"):
            return True

        client = self._pan.get_client()
        by_hash: dict[str, dict[str, object]] = {}
        try:
            for page in range(1, 4):
                listing = await client.get_task_list(page=page)
                for task in listing.get("tasks") or []:
                    if isinstance(task, dict) and task.get("info_hash"):
                        by_hash[str(task["info_hash"]).upper()] = task
                page_count = int(listing.get("page_count") or 1)
                if page >= page_count:
                    break
        except Exception as exc:
            logger.warning("115 get_task_list failed: %s", type(exc).__name__)
            return True

        cfg = self._pan.config_store.load()
        completed_paths: list[str] = []

        with self._database.session() as session:
            repo = Repository(session)
            running = repo.list_running_pan_offline_tasks()
            for row in running:
                remote = by_hash.get(row.info_hash.upper())
                if remote is None:
                    continue
                local_status = str(remote.get("local_status") or "running")
                progress = float(remote.get("progress") or 0.0)
                file_id = remote.get("file_id")
                name = remote.get("name")
                if local_status == "running":
                    repo.update_pan_offline_task(
                        row,
                        progress=progress,
                        file_id=str(file_id) if file_id else None,
                        remote_name=str(name) if isinstance(name, str) and name else row.remote_name,
                    )
                    continue
                if local_status == "failed":
                    repo.update_pan_offline_task(
                        row,
                        status="failed",
                        progress=progress,
                        error="115 offline task failed",
                        remote_name=str(name) if isinstance(name, str) and name else row.remote_name,
                    )
                    continue
                work = repo.get_work(row.work_id)
                work_code = work.primary_code if work is not None else None
                remote_name = str(name) if isinstance(name, str) and name else row.remote_name
                remote_path = remote_name
                if file_id:
                    try:
                        info = await client.get_folder_info(str(file_id))
                        paths = info.get("path") or info.get("paths")
                        file_name = info.get("file_name") or info.get("fn") or info.get("name")
                        if isinstance(file_name, str) and file_name:
                            remote_name = file_name
                        if isinstance(paths, list) and paths:
                            parts: list[str] = []
                            for part in paths:
                                if not isinstance(part, dict):
                                    continue
                                label = part.get("name") or part.get("file_name")
                                if isinstance(label, str) and label and label not in _ROOT_NAMES:
                                    parts.append(label)
                            if remote_name:
                                parts.append(remote_name)
                            if parts:
                                remote_path = "/".join(parts)
                    except Exception:
                        pass
                strm_path = None
                try:
                    strm_path = write_offline_strm(
                        settings=cfg,
                        work_code=work_code,
                        file_name=remote_name,
                        remote_relative=remote_path,
                    )
                except Exception as exc:
                    logger.warning("STRM write failed: %s", type(exc).__name__)
                repo.update_pan_offline_task(
                    row,
                    status="done",
                    progress=100.0,
                    file_id=str(file_id) if file_id else None,
                    remote_name=remote_name,
                    remote_path=remote_path,
                    strm_path=strm_path,
                    error=None,
                )
                if strm_path:
                    completed_paths.append(strm_path)

        for path in completed_paths:
            try:
                connector = MediaServerConnector(
                    settings=self._media_server_store.load(),
                    client=self._http,
                )
                await connector.refresh_path(path)
            except Exception:
                logger.warning("media server refresh failed for strm", exc_info=True)

        if self._task_events is not None:
            self._task_events.notify()
        return True
