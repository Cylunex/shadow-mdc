import json
import shutil
import subprocess
from pathlib import Path


def probe_duration(path: str | Path) -> float | None:
    executable = shutil.which("ffprobe")
    if executable is None:
        return None
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(path),
        ],
        capture_output=True,
        check=False,
        text=True,
        timeout=30,
    )
    if result.returncode != 0:
        return None
    try:
        payload = json.loads(result.stdout)
        format_data = payload.get("format")
        if not isinstance(format_data, dict):
            return None
        value = format_data.get("duration")
        return float(value) if value is not None else None
    except (ValueError, TypeError, json.JSONDecodeError):
        return None
