"""Same-directory temp → rename replace for Emby sidecars.

Ideas from peer media exporters (GPL miyabi: ideas only, no code copy): never
unlink the destination first; write a sibling temp, then replace; on failure
leave the previous file intact; follow symlinks to the real target; preserve
existing permission bits when replacing.
"""

from __future__ import annotations

import os
import stat
import tempfile
from collections.abc import Callable
from pathlib import Path


def resolve_write_target(destination: Path) -> Path:
    """Return the real file path to replace (follow a final symlink once)."""

    path = Path(destination)
    if path.is_symlink():
        try:
            return path.resolve(strict=False)
        except OSError:
            return path
    return path


def write_bytes(destination: Path, payload: bytes, *, fsync: bool = True) -> Path:
    """Atomically publish ``payload`` at ``destination`` (same-dir temp + replace)."""

    target = resolve_write_target(Path(destination))
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = _existing_mode(target)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    temp_path = Path(temporary)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            if fsync:
                os.fsync(stream.fileno())
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def write_text(destination: Path, text: str, *, encoding: str = "utf-8", newline: str = "\n") -> Path:
    """UTF-8 text variant of :func:`write_bytes`."""

    if newline and newline != "\n":
        text = text.replace("\n", newline)
    return write_bytes(destination, text.encode(encoding))


def copy_file(source: Path, destination: Path) -> Path:
    """Copy ``source`` onto ``destination`` via same-dir temp + replace."""

    target = resolve_write_target(Path(destination))
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = _existing_mode(target)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        with Path(source).open("rb") as src, temp_path.open("wb") as dst:
            while True:
                chunk = src.read(1 << 16)
                if not chunk:
                    break
                dst.write(chunk)
            dst.flush()
            os.fsync(dst.fileno())
        mode = existing_mode
        if mode is None:
            try:
                mode = stat.S_IMODE(Path(source).stat().st_mode)
            except OSError:
                mode = None
        if mode is not None:
            os.chmod(temp_path, mode)
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def write_via(
    destination: Path,
    writer: Callable[[Path], None],
) -> Path:
    """Run ``writer(temp_path)`` then rename over ``destination``."""

    target = resolve_write_target(Path(destination))
    target.parent.mkdir(parents=True, exist_ok=True)
    existing_mode = _existing_mode(target)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{target.name}.",
        suffix=".tmp",
        dir=target.parent,
    )
    os.close(descriptor)
    temp_path = Path(temporary)
    try:
        writer(temp_path)
        if existing_mode is not None:
            os.chmod(temp_path, existing_mode)
        os.replace(temp_path, target)
    except Exception:
        temp_path.unlink(missing_ok=True)
        raise
    return target


def _existing_mode(path: Path) -> int | None:
    try:
        if path.is_file() or path.is_symlink():
            return stat.S_IMODE(path.stat().st_mode)
    except OSError:
        return None
    return None
