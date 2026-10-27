"""In-process SSE fan-out for TaskRun progress snapshots."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field


@dataclass
class TaskEventHub:
    """Wake SSE subscribers when task rows may have changed."""

    _waiters: dict[asyncio.Event, asyncio.AbstractEventLoop] = field(default_factory=dict)
    _lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    async def subscribe(self) -> asyncio.Event:
        event = asyncio.Event()
        async with self._lock:
            self._waiters[event] = asyncio.get_running_loop()
        event.set()
        return event

    async def unsubscribe(self, event: asyncio.Event) -> None:
        async with self._lock:
            self._waiters.pop(event, None)

    def notify(self) -> None:
        for waiter, loop in list(self._waiters.items()):
            try:
                loop.call_soon_threadsafe(waiter.set)
            except RuntimeError:
                # A disconnected subscriber's loop may already have closed.
                continue

    async def changes(self, event: asyncio.Event, *, heartbeat_seconds: float = 15.0) -> AsyncGenerator[str]:
        """Yield 'tasks' or 'ping' tokens until the caller stops iterating."""

        while True:
            try:
                await asyncio.wait_for(event.wait(), timeout=heartbeat_seconds)
                event.clear()
                yield "tasks"
            except TimeoutError:
                yield "ping"
