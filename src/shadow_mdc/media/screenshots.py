from __future__ import annotations

import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class ScreenshotCapture:
    """Paths for artwork generated from one concrete local media asset."""

    fanart: Path
    poster: Path
    timestamp_seconds: float


def capture_screenshot(
    source: str | Path,
    output_dir: str | Path,
    *,
    duration_seconds: float | None,
) -> ScreenshotCapture:
    """Capture a stable local frame; STRM pointers are intentionally unsupported."""

    source_path = Path(source).resolve()
    if source_path.suffix.casefold() == ".strm":
        raise ValueError("STRM is a remote pointer and does not require a screenshot")
    if not source_path.is_file():
        raise FileNotFoundError(source_path)
    executable = shutil.which("ffmpeg")
    if executable is None:
        raise RuntimeError("ffmpeg is required to generate non-JAV screenshots")

    destination = Path(output_dir).resolve()
    destination.mkdir(parents=True, exist_ok=True)
    fanart = destination / "fanart.jpg"
    poster = destination / "poster.jpg"
    temporary = destination / "fanart.capture.jpg"
    timestamp = _capture_timestamp(duration_seconds)
    try:
        result = subprocess.run(
            [
                executable,
                "-hide_banner",
                "-loglevel",
                "error",
                "-ss",
                f"{timestamp:.3f}",
                "-i",
                str(source_path),
                "-frames:v",
                "1",
                "-vf",
                "scale=1280:-2:force_original_aspect_ratio=decrease",
                "-q:v",
                "2",
                "-y",
                str(temporary),
            ],
            capture_output=True,
            check=False,
            text=True,
            timeout=90,
        )
        if result.returncode != 0 or not temporary.is_file() or temporary.stat().st_size == 0:
            detail = result.stderr.strip() or "ffmpeg did not produce an image"
            raise RuntimeError(f"screenshot capture failed: {detail[:500]}")
        temporary.replace(fanart)
        shutil.copy2(fanart, poster)
    finally:
        temporary.unlink(missing_ok=True)
    return ScreenshotCapture(fanart=fanart, poster=poster, timestamp_seconds=timestamp)


def _capture_timestamp(duration_seconds: float | None) -> float:
    if duration_seconds is None or duration_seconds <= 0:
        return 10.0
    if duration_seconds <= 4:
        return duration_seconds * 0.5
    # Avoid opening/closing credits and avoid very long seeks on remote disks.
    return min(max(3.0, duration_seconds * 0.2), 300.0, duration_seconds - 1.0)
