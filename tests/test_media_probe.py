import json
import subprocess
from pathlib import Path

import pytest

from shadow_mdc.media.probe import probe_media_info


def test_probe_media_info_extracts_file_level_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    payload = {
        "format": {
            "duration": "3723.5",
            "format_name": "matroska,webm",
            "bit_rate": "12500000",
        },
        "streams": [
            {
                "codec_type": "video",
                "codec_name": "hevc",
                "width": 3840,
                "height": 2160,
                "avg_frame_rate": "24000/1001",
                "bit_rate": "12000000",
                "bits_per_raw_sample": "10",
                "color_transfer": "smpte2084",
            },
            {
                "codec_type": "audio",
                "codec_name": "aac",
                "bit_rate": "256000",
                "channels": 2,
                "sample_rate": "48000",
            },
        ],
    }
    monkeypatch.setattr("shadow_mdc.media.probe.shutil.which", lambda command: "ffprobe")
    monkeypatch.setattr(
        "shadow_mdc.media.probe.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args=["ffprobe"],
            returncode=0,
            stdout=json.dumps(payload),
            stderr="",
        ),
    )

    info = probe_media_info(tmp_path / "movie.mkv")

    assert info.duration_seconds == 3723.5
    assert info.container == "matroska,webm"
    assert info.video_codec == "hevc"
    assert info.audio_codec == "aac"
    assert (info.width, info.height, info.quality_label) == (3840, 2160, "4K")
    assert info.frame_rate == pytest.approx(23.976, abs=0.001)
    assert info.bit_depth == 10
    assert info.hdr_format == "HDR10/PQ"
