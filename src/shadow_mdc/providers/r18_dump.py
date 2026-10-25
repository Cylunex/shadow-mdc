"""Offline r18.dev dump provider (local SQLite; no network)."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from ..services.r18_dump import PROVIDER_ID, R18DumpStore

logger = logging.getLogger(__name__)


class R18DumpWorker:
    """Single daemon thread that owns blocking SQLite dump lookups.

    Keeps the event loop free; ``stop()`` signals the thread and joins it with a
    bounded timeout on app shutdown.
    """

    def __init__(self, name: str = "r18-dump-worker"):
        self._stop = threading.Event()
        self._jobs: queue.Queue[tuple[Future[Any], Callable[[], Any]] | None] = queue.Queue()
        self._thread = threading.Thread(target=self._run, name=name, daemon=True)
        self._thread.start()

    @property
    def alive(self) -> bool:
        return self._thread.is_alive()

    def _run(self) -> None:
        while not self._stop.is_set():
            item = self._jobs.get()
            if item is None:
                break
            future, call = item
            if not future.set_running_or_notify_cancel():
                continue
            try:
                future.set_result(call())
            except BaseException as exc:
                future.set_exception(exc)
        # Drain: fail anything queued after stop so no caller hangs forever.
        while True:
            try:
                item = self._jobs.get_nowait()
            except queue.Empty:
                break
            if item is not None:
                item[0].cancel()

    def submit(self, call: Callable[[], Any]) -> Future[Any]:
        future: Future[Any] = Future()
        if self._stop.is_set():
            future.cancel()
            return future
        self._jobs.put((future, call))
        return future

    def stop(self, timeout: float = 5.0) -> bool:
        """Signal stop and join; returns True when the thread exited in time."""

        self._stop.set()
        self._jobs.put(None)
        self._thread.join(timeout)
        if self._thread.is_alive():
            logger.warning("r18 dump worker did not stop within %.1fs", timeout)
            return False
        return True


class R18DumpProvider:
    """Looks up JAV metadata from an imported r18.dev dump SQLite file."""

    def __init__(self, db_path: Path | None = None, store: R18DumpStore | None = None):
        if store is None and db_path is None:
            raise ValueError("r18 dump provider requires db_path or store")
        self._db_path = Path(db_path) if db_path is not None else None
        self._store = store
        self._owns_store = store is None
        self._worker: R18DumpWorker | None = None
        self._closed = False

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=PROVIDER_ID,
            name="R18.dev dump (offline)",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    def _get_store(self) -> R18DumpStore | None:
        if self._store is not None:
            return self._store
        if self._db_path is None or not self._db_path.is_file():
            return None
        self._store = R18DumpStore(self._db_path)
        return self._store

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        code, family = extract_code(requested)
        if code is None or family is not ContentFamily.JAV or code.startswith("FC2-"):
            return []
        store = self._get_store()
        if store is None:
            return []
        if self._closed:
            return []
        if self._worker is None:
            self._worker = R18DumpWorker()
        record = await asyncio.wrap_future(self._worker.submit(lambda: store.build_record(code)))
        return [record] if record is not None else []

    def shutdown(self, timeout: float = 5.0) -> bool:
        """Stop + join the dump worker (bounded), then close the store."""

        self._closed = True
        stopped = True
        if self._worker is not None:
            stopped = self._worker.stop(timeout)
            self._worker = None
        if stopped:
            self.close()
        return stopped

    def close(self) -> None:
        if self._owns_store and self._store is not None:
            self._store.close()
            self._store = None

