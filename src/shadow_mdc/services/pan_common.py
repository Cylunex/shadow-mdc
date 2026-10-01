"""Backend-neutral pan primitives shared by the 115 Open and OpenList backends."""

from __future__ import annotations

import asyncio
import random
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Any

# Cold directory walks are paced softer than the generic limiter (~2-3 req/s).
SCAN_PACE_SECONDS = 0.35
SCAN_JITTER_SECONDS = 0.15
_VIDEO_EXTENSIONS = (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".m2ts", ".mov", ".flv", ".webm", ".iso", ".rmvb")


class PanNotConfiguredError(RuntimeError):
    """Raised when pan features are invoked before credentials/config."""


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
