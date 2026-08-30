import json
import shutil
import subprocess
from pathlib import Path

from ..domain import MediaTechnicalInfo


def probe_duration(path: str | Path) -> float | None:
    return probe_media_info(path).duration_seconds


def probe_media_info(path: str | Path) -> MediaTechnicalInfo:
    executable = shutil.which("ffprobe")
    if executable is None:
        return MediaTechnicalInfo()
    result = subprocess.run(
        [
            executable,
            "-v",
            "error",
            "-show_format",
            "-show_streams",
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
        return MediaTechnicalInfo()
    try:
        payload = json.loads(result.stdout)
        format_data = payload.get("format")
        if not isinstance(format_data, dict):
            format_data = {}
        streams = payload.get("streams")
        stream_values = streams if isinstance(streams, list) else []
        video = next(
            (item for item in stream_values if isinstance(item, dict) and item.get("codec_type") == "video"),
            {},
        )
        audio = next(
            (item for item in stream_values if isinstance(item, dict) and item.get("codec_type") == "audio"),
            {},
        )
        width = _integer(video.get("width"))
        height = _integer(video.get("height"))
        transfer = _text(video.get("color_transfer"))
        return MediaTechnicalInfo(
            duration_seconds=_number(format_data.get("duration")),
            container=_text(format_data.get("format_name")),
            video_codec=_text(video.get("codec_name")),
            audio_codec=_text(audio.get("codec_name")),
            width=width,
            height=height,
            frame_rate=_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            overall_bitrate=_integer(format_data.get("bit_rate")),
            video_bitrate=_integer(video.get("bit_rate")),
            audio_bitrate=_integer(audio.get("bit_rate")),
            bit_depth=_integer(video.get("bits_per_raw_sample")),
            audio_channels=_integer(audio.get("channels")),
            audio_sample_rate=_integer(audio.get("sample_rate")),
            hdr_format=_hdr_format(transfer),
            quality_label=_quality_label(width, height),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return MediaTechnicalInfo()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> float | None:
    if not isinstance(value, (str, int, float)):
        return None
    try:
        return float(value)
    except ValueError:
        return None


def _integer(value: object) -> int | None:
    if not isinstance(value, (str, int, float)) or value in {"", "N/A"}:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _frame_rate(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    numerator, separator, denominator = value.partition("/")
    try:
        return float(numerator) / float(denominator) if separator else float(numerator)
    except (ValueError, ZeroDivisionError):
        return None


def _quality_label(width: int | None, height: int | None) -> str | None:
    edge = max(width or 0, height or 0)
    if edge >= 7680:
        return "8K"
    if edge >= 3840:
        return "4K"
    if height and height >= 2160:
        return "4K"
    if height and height >= 1440:
        return "1440p"
    if height and height >= 1080:
        return "1080p"
    if height and height >= 720:
        return "720p"
    return f"{height}p" if height else None


def _hdr_format(transfer: str | None) -> str | None:
    return {
        "smpte2084": "HDR10/PQ",
        "arib-std-b67": "HLG",
    }.get((transfer or "").casefold())
