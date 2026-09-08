"""In-process SSE fan-out for TaskRun progress snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from dataclasses import dataclass, field


@dataclass
class TaskEventHub:
    """Wake SSE subscribers when task rows may have changed."""

    _waiters: list[asyncio.Event] = field(default_factory=list)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        async with self._lock:
            self._waiters.append(event)
        event.set()
        return event

    async def unsubscribe(self, event: asyncio.Event) -> None:
        async with self._lock:
            if event in self._waiters:
                self._waiters.remove(event)

    def notify(self) -> None:
        for waiter in list(self._waiters):
            waiter.set()

    async def changes(self, event: asyncio.Event, *, heartbeat_seconds: float = 15.0) -> AsyncIterator[str]:
        """Yield 'tasks' or 'ping' tokens until the caller stops iterating."""

        while True:
            event.clear()
            try:
                await asyncio.wait_for(event.wait(), timeout=heartbeat_seconds)
                yield "tasks"
            except TimeoutError:
                yield "ping"
