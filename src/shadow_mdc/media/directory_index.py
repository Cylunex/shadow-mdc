"""Cached directory listings for hot read paths.

List payloads used to answer "does this artwork stem exist?" with one ``glob`` per
work plus one ``stat`` per candidate file — tens of thousands of syscalls per
request on a full catalog. One ``os.scandir`` snapshot per directory answers every
question about that directory instead.

Snapshots live for ``TTL_SECONDS``. Callers that must not miss a freshly written
file either :func:`invalidate` the directory (writers), or compare
:func:`generation` around a :func:`listing` call to tell a fresh scan from a cached
one, and :func:`refresh` when a cached snapshot disagrees with the disk.
"""

from __future__ import annotations

import os
import threading
import time
from dataclasses import dataclass
from pathlib import Path

TTL_SECONDS = 30.0
MAX_ENTRIES = 60_000


@dataclass(frozen=True)
class DirectoryListing:
    files: frozenset[str]
    stems: frozenset[str]
    dirs: frozenset[str]
    generation: int
    expires_at: float


_listings: dict[str, DirectoryListing] = {}
_lock = threading.Lock()
_generation = 0


def generation() -> int:
    """Current scan generation; every :class:`DirectoryListing` carries its own."""

    return _generation


def _next_generation() -> int:
    global _generation
    _generation += 1
    return _generation


def invalidate(directory: Path) -> None:
    """Drop the cached listing for ``directory`` (call after writing files)."""

    with _lock:
        _listings.pop(str(directory), None)


def clear() -> None:
    with _lock:
        _listings.clear()


def _scan(directory: Path) -> tuple[set[str], set[str], set[str]] | None:
    files: set[str] = set()
    stems: set[str] = set()
    dirs: set[str] = set()
    try:
        with os.scandir(directory) as entries:
            for entry in entries:
                try:
                    if entry.is_dir():
                        dirs.add(entry.name)
                        continue
                    if not entry.is_file():
                        continue
                except OSError:
                    continue
                files.add(entry.name)
                stem, dot, _suffix = entry.name.rpartition(".")
                stems.add(stem if dot and stem else entry.name)
    except OSError:
        return None
    return files, stems, dirs


def listing(directory: Path) -> DirectoryListing:
    key = str(directory)
    now = time.monotonic()
    with _lock:
        cached = _listings.get(key)
        if cached is not None and cached.expires_at > now:
            return cached
    scanned = _scan(directory)
    if scanned is None:
        # Missing / unreadable directory: never cache that, a directory can be
        # created at any moment (a fresh work's artwork dir).
        return DirectoryListing(
            files=frozenset(),
            stems=frozenset(),
            dirs=frozenset(),
            generation=_next_generation(),
            expires_at=0.0,
        )
    files, stems, dirs = scanned
    snapshot = DirectoryListing(
        files=frozenset(files),
        stems=frozenset(stems),
        dirs=frozenset(dirs),
        generation=_next_generation(),
        expires_at=now + TTL_SECONDS,
    )
    with _lock:
        if len(_listings) > MAX_ENTRIES:
            _listings.clear()
        _listings[key] = snapshot
    return snapshot


def has_file(directory: Path, name: str) -> bool:
    return name in listing(directory).files


def stems(directory: Path) -> frozenset[str]:
    return listing(directory).stems


def dirs(directory: Path) -> frozenset[str]:
    return listing(directory).dirs


def file_with_stem(directory: Path, stem: str) -> Path | None:
    """Deterministic replacement for ``next(directory.glob(f"{stem}.*"))``."""

    name = name_with_stem(listing(directory).files, stem)
    return directory / name if name is not None else None


def name_with_stem(files: frozenset[str], stem: str) -> str | None:
    names = sorted(name for name in files if name.rpartition(".")[0] == stem)
    return names[0] if names else None


def refresh(directory: Path) -> DirectoryListing:
    """Rescan ``directory`` immediately (used when a cached snapshot disagrees)."""

    invalidate(directory)
    return listing(directory)
