"""Durable, debounced, batched Emby/Jellyfin ``Library/Media/Updated`` notifications.

Export / rewrite / delete reconcile / organize enqueue changed paths; after a
quiet period (``debounce_seconds``, capped by ``max_wait_seconds``) all pending
paths go out in one ``POST /Library/Media/Updated`` call.

Durability (own implementation of the idea in the peer notes): the queue is an
upsert-by-path map persisted to a small JSON file, each entry carrying a
revision. A path is only dropped after Emby accepted the batch *and* nobody
re-enqueued it meanwhile; failed batches back off exponentially per entry and
survive restarts. Incremental exports only ever send path updates — this module
never falls back to a full ``Library/Refresh``.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from .media_server import MediaServerSettings

logger = logging.getLogger(__name__)

MAX_UPDATES_PER_CALL = 200
RETRY_BASE_SECONDS = 15.0
RETRY_MAX_SECONDS = 30 * 60.0
MAX_QUEUED_PATHS = 5000
_PRIORITY = {"Deleted": 2, "Created": 1, "Modified": 0}
_VALID_TYPES = frozenset(_PRIORITY)


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


@dataclass(slots=True)
class PendingUpdate:
    update_type: str
    revision: int
    attempts: int = 0
    # Wall clock (time.time()) before which this entry is not retried.
    next_attempt: float = 0.0
    last_error: str | None = None


class EmbyNotifier:
    """Persistent path queue flushed in one batch after a debounce window."""

    def __init__(
        self,
        *,
        load_settings: Callable[[], MediaServerSettings],
        client: httpx.AsyncClient,
        debounce_seconds: float | None = None,
        max_wait_seconds: float = 60.0,
        state_path: Path | None = None,
        retry_base_seconds: float = RETRY_BASE_SECONDS,
        retry_max_seconds: float = RETRY_MAX_SECONDS,
        wall_clock: Callable[[], float] = time.time,
    ) -> None:
        self._load_settings = load_settings
        self._client = client
        self._debounce_override = debounce_seconds
        self._max_wait = max_wait_seconds
        self._state_path = state_path
        self._retry_base = retry_base_seconds
        self._retry_max = retry_max_seconds
        self._wall = wall_clock
        self._pending: dict[str, PendingUpdate] = {}
        self._revision = 0
        self._first_at: float | None = None
        self._last_at: float = 0.0
        self._wake = asyncio.Event()
        self._task: asyncio.Task[None] | None = None
        self._stopping = False
        self._flush_lock = asyncio.Lock()
        self.last_result: NotifyBatchResult | None = None
        self._load()

    # ------------------------------------------------------------ persistence

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            logger.warning("emby notify queue unreadable; starting empty")
            return
        if not isinstance(payload, dict):
            return
        self._revision = int(payload.get("revision") or 0)
        for item in payload.get("pending") or []:
            if not isinstance(item, dict):
                continue
            path = item.get("path")
            kind = item.get("update_type")
            if not isinstance(path, str) or not path or kind not in _VALID_TYPES:
                continue
            self._pending[path] = PendingUpdate(
                update_type=str(kind),
                revision=int(item.get("revision") or 0),
                attempts=int(item.get("attempts") or 0),
                next_attempt=float(item.get("next_attempt") or 0.0),
                last_error=str(item["last_error"]) if item.get("last_error") else None,
            )
            self._revision = max(self._revision, self._pending[path].revision)
        if self._pending:
            logger.info("emby notify: resumed %d pending path(s) from disk", len(self._pending))

    def _save(self) -> None:
        if self._state_path is None:
            return
        payload = {
            "version": 1,
            "revision": self._revision,
            "pending": [
                {
                    "path": path,
                    "update_type": item.update_type,
                    "revision": item.revision,
                    "attempts": item.attempts,
                    "next_attempt": item.next_attempt,
                    "last_error": item.last_error,
                }
                for path, item in self._pending.items()
            ],
        }
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_name(f".{self._state_path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            logger.warning("emby notify queue could not be saved", exc_info=True)

    # ------------------------------------------------------------------ queue

    @property
    def pending(self) -> dict[str, str]:
        return {path: item.update_type for path, item in self._pending.items()}

    def pending_details(self) -> dict[str, PendingUpdate]:
        return dict(self._pending)

    def _debounce(self) -> float:
        if self._debounce_override is not None:
            return self._debounce_override
        return float(getattr(self._load_settings(), "notify_debounce_seconds", 5.0))

    def enqueue(self, paths: list[str] | tuple[str, ...], update_type: str = "Modified") -> None:
        if update_type not in _VALID_TYPES:
            raise ValueError(f"unsupported update type: {update_type}")
        now = time.monotonic()
        changed = False
        for path in paths:
            if not path:
                continue
            previous = self._pending.get(path)
            kind = update_type
            # Deleted/Created win over a generic Modified for the same path,
            # but a fresh Created after a Deleted (re-export) is a real change.
            if previous is not None and update_type == "Modified" and previous.update_type != "Modified":
                kind = previous.update_type
            self._revision += 1
            # Upsert-by-path: a new revision resets the backoff for that path.
            self._pending[path] = PendingUpdate(update_type=kind, revision=self._revision)
            changed = True
        if not changed:
            return
        while len(self._pending) > MAX_QUEUED_PATHS:
            oldest = min(self._pending, key=lambda key: self._pending[key].revision)
            self._pending.pop(oldest, None)
        self._save()
        if self._first_at is None:
            self._first_at = now
        self._last_at = now
        self._wake.set()

    def retry_now(self) -> int:
        """Clear backoff on every pending path (manual scan / settings saved)."""

        for item in self._pending.values():
            item.next_attempt = 0.0
        if self._pending:
            self._save()
            self._wake.set()
        return len(self._pending)

    def start(self) -> None:
        if self._task is None:
            self._stopping = False
            self._task = asyncio.create_task(self._loop(), name="shadow-mdc-emby-notify")
            if self._pending:
                self._wake.set()

    async def stop(self, *, flush: bool = True) -> None:
        self._stopping = True
        self._wake.set()
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task
            self._task = None
        if flush and self._due_entries():
            with contextlib.suppress(Exception):
                await self.flush()
        self._save()

    def _due_entries(self) -> list[tuple[str, PendingUpdate]]:
        wall = self._wall()
        return [(path, item) for path, item in self._pending.items() if item.next_attempt <= wall]

    async def flush(self) -> NotifyBatchResult:
        async with self._flush_lock:
            due = self._due_entries()
            self._first_at = None
            if not due:
                return NotifyBatchResult(False, True, 0, "nothing due")
            sent_revisions = {path: item.revision for path, item in due}
            batch = [(path, item.update_type) for path, item in due]
            settings = self._load_settings()
            result = await post_media_updated(settings, self._client, batch)
            self.last_result = result
            if result.ok:
                # Accepted (or notifications disabled): drop what we sent unless re-enqueued.
                for path, revision in sent_revisions.items():
                    current = self._pending.get(path)
                    if current is not None and current.revision == revision:
                        del self._pending[path]
            else:
                logger.warning("emby notify failed (%d path(s) kept for retry): %s", len(due), result.detail)
                wall = self._wall()
                accepted = result.sent
                for index, (path, revision) in enumerate(sent_revisions.items()):
                    current = self._pending.get(path)
                    if current is None or current.revision != revision:
                        continue
                    if index < accepted:
                        del self._pending[path]
                        continue
                    current.attempts += 1
                    delay = min(self._retry_base * (2 ** min(current.attempts - 1, 16)), self._retry_max)
                    current.next_attempt = wall + delay
                    current.last_error = result.detail[:300]
            self._save()
            return result

    def due_in(self, now: float | None = None) -> float | None:
        if not self._pending:
            return None
        current = time.monotonic() if now is None else now
        debounce_wait = 0.0
        if self._first_at is not None:
            quiet_deadline = self._last_at + self._debounce()
            hard_deadline = self._first_at + self._max_wait
            debounce_wait = max(0.0, min(quiet_deadline, hard_deadline) - current)
        wall = self._wall()
        earliest = min(item.next_attempt for item in self._pending.values())
        return max(debounce_wait, earliest - wall, 0.0)

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
                await asyncio.sleep(1.0)
