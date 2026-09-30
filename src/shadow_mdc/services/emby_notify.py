"""Debounced, batched Emby/Jellyfin ``Library/Media/Updated`` notifications.

Export / rewrite / delete reconcile enqueue changed paths; after a quiet period
(``debounce_seconds``, capped by ``max_wait_seconds``) all pending paths go out in
one ``POST /Library/Media/Updated`` call instead of one request (or a full library
refresh) per title.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

import httpx

from .media_server import MediaServerSettings

logger = logging.getLogger(__name__)

MAX_UPDATES_PER_CALL = 200


@dataclass(frozen=True, slots=True)
class NotifyBatchResult:
    attempted: bool
    ok: bool
    sent: int
    detail: str


async def post_media_updated(
    settings: MediaServerSettings,
    client: httpx.AsyncClient,
    updates: list[tuple[str, str]],
) -> NotifyBatchResult:
    """Send ``(path, update_type)`` pairs; update_type is Created|Modified|Deleted."""

    if not updates:
        return NotifyBatchResult(False, True, 0, "nothing to send")
    if not settings.enabled:
        return NotifyBatchResult(False, True, 0, "disabled")
    if not settings.base_url or not settings.api_key:
        return NotifyBatchResult(False, False, 0, "missing base_url or api_key")
    base = settings.base_url.rstrip("/")
    headers = {"X-Emby-Token": settings.api_key}
    sent = 0
    try:
        for start in range(0, len(updates), MAX_UPDATES_PER_CALL):
            chunk = updates[start : start + MAX_UPDATES_PER_CALL]
            response = await client.post(
                f"{base}/Library/Media/Updated",
                headers=headers,
                json={"Updates": [{"Path": path, "UpdateType": kind} for path, kind in chunk]},
                timeout=20.0,
            )
            response.raise_for_status()
            sent += len(chunk)
    except httpx.HTTPError as exc:
        return NotifyBatchResult(True, False, sent, f"{type(exc).__name__}: {exc}")
    return NotifyBatchResult(True, True, sent, "accepted")


class EmbyNotifier:
    """Collects paths and flushes them in one batch after a debounce window."""

    def __init__(
        self,
        *,
        load_settings: Callable[[], MediaServerSettings],
        client: httpx.AsyncClient,
        debounce_seconds: float | None = None,
        max_wait_seconds: float = 60.0,
    ) -> None:
        self._load_settings = load_settings
        self._client = client
        self._debounce_override = debounce_seconds
        self._max_wait = max_wait_seconds
        self._pending: dict[str, str] = {}
        self._first_at: float | None = None
        self._last_at: float = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self.last_result: NotifyBatchResult | None = None

    @property
    def pending(self) -> dict[str, str]:
        return dict(self._pending)

    def _debounce(self) -> float:
        if self._debounce_override is not None:
            return self._debounce_override
        return float(getattr(self._load_settings(), "notify_debounce_seconds", 5.0))

    def enqueue(self, paths: list[str] | tuple[str, ...], update_type: str = "Modified") -> None:
        now = time.monotonic()
        for path in paths:
            if not path:
                continue
            previous = self._pending.get(path)
            # Deleted/Created win over a generic Modified for the same path.
            if previous in {"Deleted", "Created"} and update_type == "Modified":
                continue
            self._pending[path] = update_type
        if self._pending:
            if self._first_at is None:
                self._first_at = now
            self._last_at = now
            self._wake.set()

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="shadow-mdc-emby-notify")

    async def stop(self, *, flush: bool = True) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if flush and self._pending:
            with contextlib.suppress(Exception):
                await self.flush()

    async def flush(self) -> NotifyBatchResult:
        batch = list(self._pending.items())
        self._pending.clear()
        self._first_at = None
        result = await post_media_updated(self._load_settings(), self._client, batch)
        self.last_result = result
        if result.attempted and not result.ok:
            logger.warning("emby notify failed: %s", result.detail)
        return result

    def due_in(self, now: float | None = None) -> float | None:
        if not self._pending or self._first_at is None:
            return None
        current = time.monotonic() if now is None else now
        quiet_deadline = self._last_at + self._debounce()
        hard_deadline = self._first_at + self._max_wait
        return max(0.0, min(quiet_deadline, hard_deadline) - current)

    async def _loop(self) -> None:
        while not self._stopping:
            wait = self.due_in()
            if wait is None:
                self._wake.clear()
                await self._wake.wait()
                continue
            if wait > 0:
                self._wake.clear()
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=wait)
                continue
            try:
                await self.flush()
            except Exception:
                logger.exception("emby notify flush crashed")
