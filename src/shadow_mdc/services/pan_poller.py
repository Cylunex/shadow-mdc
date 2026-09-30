"""Background poller for 115 offline tasks → STRM export + Emby notify.

Completed offline tasks are exported as ``{root}/{CODE}/`` with poster/fanart →
NFO → ``.strm`` last. Network calls happen outside DB sessions so the SQLite
writer lock is never held across slow 115 requests.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from ..db.repository import Database, Repository
from ..media.artwork import ArtworkStore
from .emby_notify import EmbyNotifier
from .media_server import MediaServerStore
from .pan import (
    PanService,
    PanSettings,
    RemoteVideo,
    ScanPacer,
    is_file_gone_error,
    write_offline_strm,
)
from .strm_export import (
    ReconcileResult,
    RewriteResult,
    export_work,
    map_to_emby_path,
    reconcile_deleted,
    rewrite_strm_tree,
)
from .task_events import TaskEventHub

if TYPE_CHECKING:
    import httpx

logger = logging.getLogger(__name__)

POLL_IDLE_SECONDS = 8.0
POLL_ACTIVE_SECONDS = 3.0
_ROOT_NAMES = frozenset({"根目录", "根目錄", "/"})


@dataclass(slots=True)
class _Completion:
    task_id: str
    work_id: str
    file_id: str | None
    name: str | None
    progress: float


@dataclass(slots=True)
class StrmMaintenanceStatus:
    last_reconcile_at: str | None = None
    last_reconcile: dict[str, object] = field(default_factory=dict)
    last_rewrite_at: str | None = None
    last_rewrite: dict[str, object] = field(default_factory=dict)
    running: str | None = None


class PanOfflinePoller:
    def __init__(
        self,
        *,
        database: Database,
        pan: PanService,
        media_server_store: MediaServerStore,
        http: httpx.AsyncClient,
        task_events: TaskEventHub | None = None,
        notifier: EmbyNotifier | None = None,
        data_dir: Path | None = None,
        artwork_max_bytes: int = 25 * 1024 * 1024,
    ):
        self._database = database
        self._pan = pan
        self._media_server_store = media_server_store
        self._http = http
        self._task_events = task_events
        self._notifier = notifier or EmbyNotifier(load_settings=media_server_store.load, client=http)
        self._owns_notifier = notifier is None
        self._data_dir = data_dir
        self._artwork_max_bytes = artwork_max_bytes
        self._task: asyncio.Task[None] | None = None
        self._stop = asyncio.Event()
        self._maintenance_lock = asyncio.Lock()
        self.maintenance = StrmMaintenanceStatus()
        self._state_path = (data_dir / "pan" / "strm-maintenance.json") if data_dir else None
        self._load_state()

    @property
    def notifier(self) -> EmbyNotifier:
        return self._notifier

    def _load_state(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if isinstance(payload, dict):
            self.maintenance.last_reconcile_at = payload.get("last_reconcile_at")
            self.maintenance.last_reconcile = payload.get("last_reconcile") or {}
            self.maintenance.last_rewrite_at = payload.get("last_rewrite_at")
            self.maintenance.last_rewrite = payload.get("last_rewrite") or {}

    def _save_state(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "last_reconcile_at": self.maintenance.last_reconcile_at,
            "last_reconcile": self.maintenance.last_reconcile,
            "last_rewrite_at": self.maintenance.last_rewrite_at,
            "last_rewrite": self.maintenance.last_rewrite,
        }
        self._state_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._state_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self._state_path)

    def start(self) -> None:
        if self._task is not None:
            return
        self._stop.clear()
        if self._owns_notifier:
            self._notifier.start()
        self._task = asyncio.create_task(self._loop(), name="shadow-mdc-pan-offline-poller")

    async def stop(self) -> None:
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if self._owns_notifier:
            await self._notifier.stop()

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
                await self.maybe_reconcile()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("STRM delete reconcile failed")
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=POLL_ACTIVE_SECONDS if had_running else POLL_IDLE_SECONDS,
                )
                break
            except TimeoutError:
                continue

    # ------------------------------------------------------------------ polling

    async def poll_once(self) -> bool:
        with self._database.session() as session:
            repo = Repository(session)
            if not repo.list_running_pan_offline_tasks():
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

        completions: list[_Completion] = []
        with self._database.session() as session:
            repo = Repository(session)
            for row in repo.list_running_pan_offline_tasks():
                remote = by_hash.get(row.info_hash.upper())
                if remote is None:
                    continue
                local_status = str(remote.get("local_status") or "running")
                progress = float(remote.get("progress") or 0.0)
                file_id = remote.get("file_id")
                name = remote.get("name")
                remote_name = str(name) if isinstance(name, str) and name else row.remote_name
                if local_status == "running":
                    repo.update_pan_offline_task(
                        row,
                        progress=progress,
                        file_id=str(file_id) if file_id else None,
                        remote_name=remote_name,
                    )
                    continue
                if local_status == "failed":
                    repo.update_pan_offline_task(
                        row,
                        status="failed",
                        progress=progress,
                        error="115 offline task failed",
                        remote_name=remote_name,
                    )
                    continue
                completions.append(
                    _Completion(
                        task_id=row.id,
                        work_id=row.work_id,
                        file_id=str(file_id) if file_id else None,
                        name=remote_name,
                        progress=progress,
                    )
                )

        cfg = self._pan.config_store.load()
        for completion in completions:
            await self._complete(completion, cfg)

        if self._task_events is not None:
            self._task_events.notify()
        return True

    async def _complete(self, completion: _Completion, cfg: PanSettings) -> None:
        client = self._pan.get_client()
        remote_name = completion.name
        remote_path = remote_name
        ancestors: list[str] = []
        if completion.file_id:
            try:
                info = await client.get_folder_info(completion.file_id)
                paths = info.get("path") or info.get("paths")
                file_name = info.get("file_name") or info.get("fn") or info.get("name")
                if isinstance(file_name, str) and file_name:
                    remote_name = file_name
                if isinstance(paths, list):
                    for part in paths:
                        if not isinstance(part, dict):
                            continue
                        label = part.get("name") or part.get("file_name")
                        if isinstance(label, str) and label and label not in _ROOT_NAMES:
                            ancestors.append(label)
                if ancestors and ancestors[-1] == remote_name:
                    ancestors.pop()
                parts = [*ancestors, remote_name] if remote_name else ancestors
                if parts:
                    remote_path = "/".join(parts)
            except Exception:
                logger.debug("115 folder info failed for %s", completion.file_id, exc_info=True)

        with self._database.session() as session:
            repo = Repository(session)
            work = repo.get_work(completion.work_id)
            work_code = work.primary_code if work is not None else None
            needs_art = work is not None and any(
                isinstance(item, dict) and item.get("url") and not item.get("local_path")
                for item in (work.artwork or [])
            )

        strm_path: str | None = None
        export_dir: Path | None = None
        if cfg.strm_enabled and cfg.strm_output_root:
            videos: list[RemoteVideo] = []
            if completion.file_id:
                try:
                    videos = await client.walk_videos(completion.file_id, pacer=ScanPacer())
                except Exception as exc:
                    logger.warning("115 walk failed for %s: %s", completion.file_id, type(exc).__name__)
            if videos:
                prefix = "/".join(ancestors)
                videos = [
                    RemoteVideo(
                        file_id=item.file_id,
                        name=item.name,
                        pick_code=item.pick_code,
                        relative_path=f"{prefix}/{item.relative_path}" if prefix else item.relative_path,
                        size=item.size,
                    )
                    for item in videos
                ]
                if needs_art and self._data_dir is not None:
                    await self._acquire_artwork(completion.work_id)
                try:
                    with self._database.session() as session:
                        repo = Repository(session)
                        work = repo.get_work(completion.work_id)
                        identities = repo.identities_for_work(work.id) if work is not None else []
                        code = work_code or Path(remote_name or "offline").stem
                        result = export_work(
                            settings=cfg,
                            code=code,
                            videos=videos,
                            work=work,
                            identities=identities,
                        )
                    export_dir = result.directory
                    strm_path = str(result.strm_paths[0]) if result.strm_paths else None
                except Exception as exc:
                    logger.warning("STRM export failed: %s: %s", type(exc).__name__, exc)
            elif cfg.strm_mode != "relay":
                # Legacy OpenList single-locator path when the walk found nothing.
                try:
                    strm_path = write_offline_strm(
                        settings=cfg,
                        work_code=work_code,
                        file_name=remote_name,
                        remote_relative=remote_path,
                    )
                    if strm_path:
                        export_dir = Path(strm_path).parent
                except Exception as exc:
                    logger.warning("STRM write failed: %s", type(exc).__name__)

        with self._database.session() as session:
            repo = Repository(session)
            row = repo.get_pan_offline_task(completion.task_id)
            if row is not None:
                repo.update_pan_offline_task(
                    row,
                    status="done",
                    progress=100.0,
                    file_id=completion.file_id,
                    remote_name=remote_name,
                    remote_path=remote_path,
                    strm_path=strm_path,
                    error=None,
                )
        if export_dir is not None:
            self._notifier.enqueue([map_to_emby_path(export_dir, cfg)], "Created")

    async def _acquire_artwork(self, work_id: str) -> None:
        assert self._data_dir is not None
        try:
            with self._database.session() as session:
                repo = Repository(session)
                work = repo.get_work(work_id)
                if work is None:
                    return
                _result, local_paths = await ArtworkStore(
                    self._data_dir / "artwork", self._http, max_bytes=self._artwork_max_bytes
                ).acquire(work)
                if local_paths:
                    repo.update_artwork_local_paths(work, local_paths)
        except Exception:
            logger.warning("artwork acquire before STRM export failed", exc_info=True)

    # ------------------------------------------------------------- maintenance

    async def rewrite(self) -> RewriteResult:
        """Rewrite .strm bodies in place for the current mode / public URL / token."""

        cfg = self._pan.config_store.load()
        if not cfg.strm_output_root:
            return RewriteResult()
        async with self._maintenance_lock:
            self.maintenance.running = "rewrite"
            try:
                result = await asyncio.to_thread(rewrite_strm_tree, Path(cfg.strm_output_root), cfg)
            finally:
                self.maintenance.running = None
        if result.rewritten:
            dirs = sorted({str(path.parent) for path in result.rewritten})
            self._notifier.enqueue([map_to_emby_path(Path(item), cfg) for item in dirs], "Modified")
        self.maintenance.last_rewrite_at = datetime.now(UTC).isoformat()
        self.maintenance.last_rewrite = {
            "scanned": result.scanned,
            "rewritten": len(result.rewritten),
            "skipped": result.skipped,
        }
        self._save_state()
        return result

    async def reconcile(self) -> ReconcileResult:
        """Remove export folders whose 115 sources are gone; notify Emby."""

        cfg = self._pan.config_store.load()
        if not cfg.strm_output_root or not self._pan.status().get("connected"):
            return ReconcileResult()
        client = self._pan.get_client()
        pacer = ScanPacer()

        async def exists(file_id: str) -> bool | None:
            await pacer.wait()
            try:
                info = await client.get_folder_info(file_id)
            except Exception as exc:
                if is_file_gone_error(exc):
                    return False
                return None
            if not info:
                return None
            return True

        async with self._maintenance_lock:
            self.maintenance.running = "reconcile"
            try:
                result = await reconcile_deleted(Path(cfg.strm_output_root), exists)
            finally:
                self.maintenance.running = None
        if result.removed:
            self._notifier.enqueue([map_to_emby_path(path, cfg) for path in result.removed], "Deleted")
        self.maintenance.last_reconcile_at = datetime.now(UTC).isoformat()
        self.maintenance.last_reconcile = {
            "checked": result.checked,
            "removed": [str(path) for path in result.removed][:50],
            "kept": result.kept,
            "unknown": result.unknown,
        }
        self._save_state()
        return result

    async def maybe_reconcile(self) -> None:
        cfg = self._pan.config_store.load()
        hours = cfg.strm_reconcile_interval_hours
        if hours <= 0 or not cfg.strm_enabled or not cfg.strm_output_root:
            return
        last = self.maintenance.last_reconcile_at
        if last:
            try:
                elapsed = time.time() - datetime.fromisoformat(last).timestamp()
            except ValueError:
                elapsed = hours * 3600
            if elapsed < hours * 3600:
                return
        else:
            # First run after enabling: start the clock instead of walking at boot.
            self.maintenance.last_reconcile_at = datetime.now(UTC).isoformat()
            self._save_state()
            return
        if not self._pan.status().get("connected"):
            return
        await self.reconcile()
