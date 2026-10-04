"""Backend-neutral pan primitives shared by the 115 Open and OpenList backends."""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any

# Cold directory walks are paced softer than the generic limiter (~2-3 req/s).
SCAN_PACE_SECONDS = 0.35
SCAN_JITTER_SECONDS = 0.15
_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".m2ts", ".mov", ".flv", ".webm", ".iso", ".rmvb")


class PanNotConfiguredError(RuntimeError):
    """Raised when pan features are invoked before credentials/config."""


class PanAuthRejectedError(PanNotConfiguredError):
    """The pan permanently rejected the stored credentials: the user must log in again.

    Raised instead of retrying, after the rejected token has been cleared, so
    background loops stop hammering the login / refresh endpoint.
    """


class SingleFlight[T]:
    """Coalesce concurrent calls per key into one in-flight task.

    Waiters share the task's result or exception. A cancelled waiter does not
    cancel the shared call (it is shielded); failures are not cached.
    """

    def __init__(self) -> None:
        self._tasks: dict[str, asyncio.Task[T]] = {}

    def in_flight(self, key: str) -> bool:
        task = self._tasks.get(key)
        return task is not None and not task.done()

    async def run(self, key: str, factory: Callable[[], Awaitable[T]]) -> T:
        task = self._tasks.get(key)
        if task is None or task.done():

            async def runner() -> T:
                return await factory()

            task = asyncio.ensure_future(runner())
            self._tasks[key] = task

            def _forget(done: asyncio.Task[T], key: str = key) -> None:
                if self._tasks.get(key) is done:
                    self._tasks.pop(key, None)
                if not done.cancelled():
                    done.exception()  # mark retrieved; waiters re-raise it

            task.add_done_callback(_forget)
        return await asyncio.shield(task)


@dataclass(frozen=True, slots=True)
class AuthRejection:
    at: str
    detail: str


class AuthRejectionStore:
    """Persisted "needs re-login" markers per backend (survive restarts)."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def _load_all(self) -> dict[str, dict[str, str]]:
        if not self._path.is_file():
            return {}
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            str(key): value
            for key, value in payload.items()
            if isinstance(value, dict) and isinstance(value.get("detail"), str)
        }

    def get(self, backend: str) -> AuthRejection | None:
        item = self._load_all().get(backend)
        if item is None:
            return None
        return AuthRejection(at=str(item.get("at") or ""), detail=str(item.get("detail") or ""))

    def _save_all(self, data: dict[str, dict[str, str]]) -> None:
        if not data:
            with contextlib.suppress(OSError):
                self._path.unlink(missing_ok=True)
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)

    def mark(self, backend: str, detail: str) -> AuthRejection:
        data = self._load_all()
        rejection = AuthRejection(at=datetime.now(UTC).isoformat(), detail=detail[:300])
        data[backend] = {"at": rejection.at, "detail": rejection.detail}
        self._save_all(data)
        return rejection

    def clear(self, backend: str) -> None:
        data = self._load_all()
        if data.pop(backend, None) is not None:
            self._save_all(data)


class PanApiError(RuntimeError):
    """115 Open API returned an error."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class PanOfflineExistsError(PanApiError):
    """115 reports the offline task already exists (typically code 10008)."""


class PanOfflineConflictError(PanApiError):
    """Offline task exists but cannot be safely reused (wrong dir / incomplete)."""


def parse_retry_after(header: str | None) -> float:
    """Return seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""

    if not header:
        return 0.0
    raw = header.strip()
    if not raw:
        return 0.0
    try:
        seconds = int(raw)
    except ValueError:
        seconds = -1
    if seconds > 0:
        return float(seconds)
    try:
        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        wait = (when - datetime.now(UTC)).total_seconds()
        return wait if wait > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


# 429 without a usable Retry-After: exponential default backoff (1, 2, 4 … s).
DEFAULT_BACKOFF_SECONDS = 1.0
MAX_BACKOFF_SECONDS = 300.0


class BackoffGate:
    """Process-wide rate-limit deadline shared by every request to one upstream.

    Any response carrying ``Retry-After`` (or a bare 429) pushes one shared
    deadline forward; every caller awaits :meth:`wait` before *each* attempt, so
    a single throttled request pauses all others instead of only retrying
    itself. After sleeping, the deadline is re-read: if a concurrent 429
    extended it meanwhile, the caller keeps waiting.
    """

    def __init__(
        self,
        name: str,
        *,
        default_seconds: float = DEFAULT_BACKOFF_SECONDS,
        max_seconds: float = MAX_BACKOFF_SECONDS,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Awaitable[None]] | None = None,
    ) -> None:
        self.name = name
        self.default_seconds = default_seconds
        self.max_seconds = max_seconds
        self._clock = clock
        self._sleep = sleep
        self._until = 0.0
        self._strikes = 0
        self.hits = 0

    def remaining(self) -> float:
        return max(0.0, self._until - self._clock())

    def note(self, status_code: int, retry_after: str | None) -> float:
        """Record a response; returns the backoff (seconds) it imposed, 0 if none."""

        wait = parse_retry_after(retry_after)
        if wait <= 0 and status_code == 429:
            self._strikes += 1
            wait = self.default_seconds * (2 ** min(self._strikes - 1, 16))
        elif status_code < 400 and wait <= 0:
            self._strikes = 0
            return 0.0
        if wait <= 0:
            return 0.0
        wait = min(wait, self.max_seconds)
        self.hits += 1
        until = self._clock() + wait
        if until > self._until:
            self._until = until
        return wait

    def reset(self) -> None:
        self._until = 0.0
        self._strikes = 0
        self.hits = 0

    @property
    def deadline(self) -> float:
        return self._until

    async def wait(self) -> float:
        """Block until the shared deadline passes; returns the deadline honoured.

        Callers that queue on another lock afterwards can compare the result with
        :attr:`deadline` to notice an extension that happened meanwhile.
        """

        while True:
            deadline = self._until
            remaining = deadline - self._clock()
            if remaining <= 0:
                return deadline
            sleep = self._sleep or asyncio.sleep
            await sleep(remaining)
            if self._until <= deadline:
                # Not extended while we slept → done (also stops a mocked sleep spinning).
                return deadline

    def snapshot(self) -> dict[str, object]:
        return {"name": self.name, "backoff_remaining": round(self.remaining(), 3), "hits": self.hits}


_SHARED_GATES: dict[str, BackoffGate] = {}


def shared_gate(name: str) -> BackoffGate:
    """The single process-wide gate for an upstream (``"115"`` / ``"openlist"``)."""

    gate = _SHARED_GATES.get(name)
    if gate is None:
        gate = _SHARED_GATES[name] = BackoffGate(name)
    return gate


def reset_shared_gates() -> None:
    for gate in _SHARED_GATES.values():
        gate.reset()


def _is_video_name(name: str) -> bool:
    lowered = name.casefold()
    return any(lowered.endswith(ext) for ext in _VIDEO_EXTENSIONS)


def is_video_name(name: str) -> bool:
    return _is_video_name(name)


class ScanPacer:
    """Per-request pacing for 115 directory walks: base delay + random jitter."""

    def __init__(
        self,
        base_seconds: float = SCAN_PACE_SECONDS,
        jitter_seconds: float = SCAN_JITTER_SECONDS,
        *,
        sleep: Any = None,
        rng: random.Random | None = None,
    ) -> None:
        self.base_seconds = base_seconds
        self.jitter_seconds = jitter_seconds
        self._sleep = sleep or asyncio.sleep
        self._rng = rng or random.Random()
        self._calls = 0

    def next_delay(self) -> float:
        return self.base_seconds + self._rng.uniform(0.0, self.jitter_seconds)

    async def wait(self) -> None:
        # No delay before the very first request of a walk.
        if self._calls:
            await self._sleep(self.next_delay())
        self._calls += 1


@dataclass(frozen=True, slots=True)
class RemoteVideo:
    file_id: str
    name: str
    pick_code: str | None = None
    relative_path: str = ""
    size: int | None = None
    # OpenList backend only: per-file `sign` from /api/fs/list or /api/fs/get.
    sign: str | None = None
