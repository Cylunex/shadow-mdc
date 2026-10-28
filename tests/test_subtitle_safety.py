from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from shadow_mdc.media.subtitle_validation import validate_srt
from shadow_mdc.services.subtitlecat import SubtitleCatFetcher, SubtitleCatPick, has_existing_subtitle

SRT = "1\n00:00:01,000 --> 00:00:02,000\n你好\n"


@pytest.mark.parametrize("encoding", ["utf-8-sig", "utf-16", "gb18030", "big5"])
def test_valid_srt_encodings(encoding: str) -> None:
    assert validate_srt(SRT.encode(encoding)) is None


@pytest.mark.parametrize(
    "payload",
    [
        b"",
        b"a" * 40 + b"-->",
        b"<html><body>1\n00:00:01,000 --> 00:00:02,000\nblocked</body></html>",
        b"1\n00:99:01,000 --> 00:00:02,000\nhello",
        b"1\n00:00:03,000 --> 00:00:02,000\nhello",
        b"1\n00:00:01,000 --> 00:00:02,000\n",
        b"a" * (8 * 1024 * 1024 + 1),
        (SRT + "\n2\n00:00:00,000 --> 00:00:01,000\nlate").encode(),
    ],
)
def test_rejects_invalid_subtitle_structure(payload: bytes) -> None:
    assert validate_srt(payload) is not None


def test_existing_subtitles_require_exact_stem_and_a_file(tmp_path: Path) -> None:
    (tmp_path / "ABC-1230.zh.srt").touch()
    (tmp_path / "ABC-123.zh.srt").mkdir()
    (tmp_path / "ABC-123-CD2.zh.srt").touch()
    assert not has_existing_subtitle(tmp_path, "ABC-123")
    (tmp_path / "ABC-123.en.forced.ass").touch()
    assert has_existing_subtitle(tmp_path, "ABC-123")


def _fetcher(
    handler: Callable[[httpx.Request], httpx.Response], monkeypatch: pytest.MonkeyPatch
) -> SubtitleCatFetcher:
    pick = SubtitleCatPick(
        code="ABC-123",
        lang="zh-CN",
        url="https://example.com/sub.srt",
        detail_url="https://example.com/detail",
        name="ABC-123",
    )
    monkeypatch.setattr(SubtitleCatFetcher, "lookup", lambda self, code: pick)
    return SubtitleCatFetcher(
        min_interval_seconds=0, client=httpx.Client(transport=httpx.MockTransport(handler))
    )


def test_download_race_preserves_existing_file_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "ABC-123.zh.srt"

    def handler(request: httpx.Request) -> httpx.Response:
        target.write_text("user subtitle")
        return httpx.Response(200, content=SRT.encode())

    result = _fetcher(handler, monkeypatch).fetch("ABC-123", tmp_path, "ABC-123")
    assert result.status == "exists"
    assert target.read_text() == "user subtitle"
    assert list(tmp_path.iterdir()) == [target]


def test_invalid_download_is_visible_without_install_or_miss_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fetcher = _fetcher(
        lambda request: httpx.Response(200, content=b"<html>fake --> subtitle</html>"), monkeypatch
    )
    fetcher.cache_path = tmp_path / "misses.json"
    result = fetcher.fetch("ABC-123", tmp_path, "ABC-123")
    assert result.status == "error" and "HTML" in (result.detail or "")
    assert list(tmp_path.iterdir()) == []


def test_download_permissions_follow_normal_file_creation(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    normal = tmp_path / "normal.srt"
    normal.write_bytes(SRT.encode())
    fetcher = _fetcher(lambda request: httpx.Response(200, content=SRT.encode()), monkeypatch)
    result = fetcher.fetch("ABC-123", tmp_path, "ABC-123")
    assert result.status == "written" and result.path is not None
    assert result.path.stat().st_mode & 0o777 == normal.stat().st_mode & 0o777
    assert not list(tmp_path.glob("*.part"))


def test_publish_failure_is_visible_and_cleans_temporary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    def unsupported(source: Path, target: Path) -> None:
        raise OSError("hard links unsupported")

    monkeypatch.setattr("shadow_mdc.services.subtitlecat.os.link", unsupported)
    fetcher = _fetcher(lambda request: httpx.Response(200, content=SRT.encode()), monkeypatch)
    result = fetcher.fetch("ABC-123", tmp_path, "ABC-123")
    assert result.status == "error" and "unsupported" in (result.detail or "")
    assert list(tmp_path.iterdir()) == []
