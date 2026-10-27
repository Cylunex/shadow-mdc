import json
import shutil
import subprocess
from math import isfinite
from pathlib import Path

from ..domain import MediaTechnicalInfo


def probe_duration(path: str | Path) -> float | None:
    return probe_media_info(path).duration_seconds


def probe_media_info(path: str | Path) -> MediaTechnicalInfo:
    executable = shutil.which("ffprobe")
    if executable is None:
        return MediaTechnicalInfo()
    try:
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
    except (OSError, subprocess.TimeoutExpired, UnicodeError):
        return MediaTechnicalInfo()
    if result.returncode != 0:
        return MediaTechnicalInfo()
    try:
        payload = json.loads(result.stdout)
        if not isinstance(payload, dict):
            return MediaTechnicalInfo()
        format_data = payload.get("format")
        if not isinstance(format_data, dict):
            format_data = {}
        streams = payload.get("streams")
        stream_values = streams if isinstance(streams, list) else []
        video = next(
            (
                item
                for item in stream_values
                if isinstance(item, dict)
                and item.get("codec_type") == "video"
                and not (
                    isinstance(item.get("disposition"), dict) and item["disposition"].get("attached_pic")
                )
            ),
            {},
        )
        audio = next(
            (item for item in stream_values if isinstance(item, dict) and item.get("codec_type") == "audio"),
            {},
        )
        width = _positive_integer(video.get("width"))
        height = _positive_integer(video.get("height"))
        transfer = _text(video.get("color_transfer"))
        return MediaTechnicalInfo(
            duration_seconds=_number(format_data.get("duration")) or _number(video.get("duration")),
            container=_text(format_data.get("format_name")),
            video_codec=_text(video.get("codec_name")),
            audio_codec=_text(audio.get("codec_name")),
            width=width,
            height=height,
            frame_rate=_frame_rate(video.get("avg_frame_rate") or video.get("r_frame_rate")),
            overall_bitrate=_integer(format_data.get("bit_rate")),
            video_bitrate=_integer(video.get("bit_rate")),
            audio_bitrate=_integer(audio.get("bit_rate")),
            bit_depth=_positive_integer(video.get("bits_per_raw_sample")),
            audio_channels=_positive_integer(audio.get("channels")),
            audio_sample_rate=_positive_integer(audio.get("sample_rate")),
            hdr_format=_hdr_format(transfer),
            quality_label=_quality_label(width, height),
        )
    except (ValueError, TypeError, json.JSONDecodeError):
        return MediaTechnicalInfo()


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _number(value: object) -> float | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool):
        return None
    try:
        number = float(value)
        return number if isfinite(number) and number >= 0 else None
    except (ValueError, OverflowError):
        return None


def _integer(value: object) -> int | None:
    if not isinstance(value, (str, int, float)) or isinstance(value, bool) or value in {"", "N/A"}:
        return None
    try:
        number = int(value)
        return number if number >= 0 else None
    except (ValueError, OverflowError):
        return None


def _positive_integer(value: object) -> int | None:
    return _integer(value) or None


def _frame_rate(value: object) -> float | None:
    if not isinstance(value, str) or not value or value == "0/0":
        return None
    numerator, separator, denominator = value.partition("/")
    try:
        rate = float(numerator) / float(denominator) if separator else float(numerator)
        return rate if isfinite(rate) and rate >= 0 else None
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
