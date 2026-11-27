"""Sources for the imported R18 dump."""

from __future__ import annotations

import asyncio
import logging
import queue
import threading
from collections.abc import Callable
from concurrent.futures import Future
from pathlib import Path
from typing import Any

import httpx
from pydantic import BaseModel, ConfigDict, Field, HttpUrl, SecretStr, ValidationError, field_validator

from ..config import Settings
from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from ..normalize_code import to_comparison_key
from ..remaster import strip_remaster_marker
from ..services.r18_dump import PROVIDER_ID, R18DumpStore
from .base import ProviderError

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

    def __init__(
        self,
        db_path: Path | None = None,
        store: R18DumpStore | None = None,
        *,
        worker_join_seconds: float = 5.0,
    ) -> None:
        if store is None and db_path is None:
            raise ValueError("r18 dump provider requires db_path or store")
        self._db_path = Path(db_path) if db_path is not None else None
        self._store = store
        self._owns_store = store is None
        self._worker: R18DumpWorker | None = None
        self._closed = False
        self._worker_join_seconds = worker_join_seconds

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=PROVIDER_ID,
            name="R18.dev dump (offline)",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
            configured=self._store is not None or (self._db_path is not None and self._db_path.is_file()),
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



    async def aclose(self) -> None:
        await asyncio.to_thread(self.shutdown, self._worker_join_seconds)


class R18ServiceConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    base_url: HttpUrl
    token: SecretStr
    attempt_timeout_seconds: float = Field(default=2.5, ge=0.1, le=10)

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, url: HttpUrl) -> HttpUrl:
        if url.username is not None or url.password is not None or url.query or url.fragment:
            raise ValueError("base URL must not contain credentials, a query or a fragment")
        return url

    @field_validator("token")
    @classmethod
    def validate_token(cls, token: SecretStr) -> SecretStr:
        raw = token.get_secret_value()
        if not raw or any(not 33 <= ord(char) <= 126 for char in raw):
            raise ValueError("token must contain printable ASCII without whitespace")
        return token


class R18ServiceProvider:
    """Look up the imported dump over a private HTTP connection."""

    def __init__(
        self, config: R18ServiceConfig, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> None:
        self._attempt_timeout = config.attempt_timeout_seconds
        self._client = httpx.AsyncClient(
            base_url=str(config.base_url),
            headers={"Authorization": f"Bearer {config.token.get_secret_value()}"},
            trust_env=False,
            follow_redirects=False,
            transport=transport,
        )

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=PROVIDER_ID,
            name="R18.dev dump (service)",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        code, family = extract_code(hints.code or hints.term)
        if code is None or family is not ContentFamily.JAV or code.startswith("FC2-"):
            return []
        for attempt in range(2):
            try:
                async with asyncio.timeout(self._attempt_timeout):
                    response = await self._client.get("lookup", params={"q": code})
            except (TimeoutError, httpx.TimeoutException):
                error = ProviderError(PROVIDER_ID, "timeout", "R18 service request deadline exceeded")
            except httpx.HTTPError as exc:
                error = ProviderError(PROVIDER_ID, "network", type(exc).__name__)
            else:
                if response.status_code != 503:
                    return self._parse_response(response, code)
                error = ProviderError(PROVIDER_ID, "network", "service not ready (HTTP 503)")
            if attempt == 0:
                await asyncio.sleep(0.15)
        raise error

    @staticmethod
    def _parse_response(response: httpx.Response, code: str) -> list[ProviderRecord]:
        status = response.status_code
        if status == 404:
            return []
        if status in {401, 403}:
            raise ProviderError(PROVIDER_ID, "authentication", f"HTTP {status}")
        if status != 200:
            raise ProviderError(PROVIDER_ID, "http", f"HTTP {status}")
        try:
            record = ProviderRecord.model_validate_json(response.content)
        except ValidationError:
            raise ProviderError(PROVIDER_ID, "invalid_response", "expected a ProviderRecord") from None
        lookup_key = to_comparison_key(strip_remaster_marker(code))
        result_key = to_comparison_key(strip_remaster_marker(record.code)) if record.code else None
        if (
            record.provider != PROVIDER_ID
            or record.family is not ContentFamily.JAV
            or result_key != lookup_key
        ):
            raise ProviderError(PROVIDER_ID, "invalid_response", "record identity does not match the lookup")
        return [record]

    async def aclose(self) -> None:
        await self._client.aclose()


def build_r18_provider(settings: Settings) -> R18DumpProvider | R18ServiceProvider:
    if settings.r18_service_config is None:
        return R18DumpProvider(
            db_path=settings.resolved_r18_dump_db(),
            worker_join_seconds=settings.r18_dump_worker_join_seconds,
        )
    try:
        config = R18ServiceConfig.model_validate_json(settings.r18_service_config.read_bytes())
    except (OSError, ValidationError):
        raise ValueError("cannot load R18 service configuration") from None
    return R18ServiceProvider(config)
