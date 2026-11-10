"""Read optional reference datasets from the library's external data directory."""

from __future__ import annotations

import gzip
import os
from functools import lru_cache
from pathlib import Path

from .config import Settings


@lru_cache(maxsize=16)
def _directories(data: str | None, reference: str | None, cwd: str) -> tuple[Path, Path]:
    settings = Settings()
    data_dir = settings.data_dir.resolve()
    return data_dir, (settings.reference_data_dir or data_dir / "reference").resolve()


def data_directories() -> tuple[Path, Path]:
    """Resolve configured paths; environment changes select a separate cache entry."""
    return _directories(
        os.environ.get("SHADOW_MDC_DATA_DIR"), os.environ.get("SHADOW_MDC_REFERENCE_DATA_DIR"), os.getcwd()
    )


def reference_path(name: str) -> Path:
    """Locate a dataset without falling back to files in the source package."""
    return data_directories()[1] / name


@lru_cache(maxsize=64)
def _read(path: Path, signature: tuple[int, int, int]) -> bytes:
    raw = path.read_bytes()
    return gzip.decompress(raw) if path.suffix == ".gz" else raw


def read_data(path: Path) -> bytes | None:
    """Missing data is optional; malformed or unreadable existing data is an error."""
    try:
        stat = path.stat()
    except FileNotFoundError:
        return None
    return _read(path, (stat.st_ino, stat.st_mtime_ns, stat.st_size))
