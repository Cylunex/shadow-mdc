"""Future cloud pan client hooks (e.g. 115 Open Platform).

Product goal includes pan media once developer approval exists. This module has
no OAuth, token storage, or content transfer. Keep providers/pan free of DB writes.

When wiring later: implement PanClient against the approved Open Platform APIs;
WorkMagnet persistence stays local and separate from any future offline submit.
"""

from __future__ import annotations

from typing import Protocol


class PanClient(Protocol):
    """Minimal surface for a future cloud pan integration."""

    async def list_directory(self, directory_id: str) -> object: ...

    async def enqueue_remote_urls(self, urls: list[str], *, directory_id: str) -> object: ...


class PanNotConfiguredError(RuntimeError):
    """Raised when pan features are invoked before approval/config."""


def pan_status() -> dict[str, object]:
    """Settings stub: unavailable until credentials exist."""

    return {
        "provider": "115",
        "configured": False,
        "available": False,
        "reason": "Open Platform developer approval pending; pan features disabled.",
    }
