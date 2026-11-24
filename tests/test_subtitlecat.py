"""SubtitleCat zh subtitle lookup: offline fixtures, drift, misses, never-raise."""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path

import httpx

from shadow_mdc.services.strm_export import SIDECAR_NAME
from shadow_mdc.services.subtitlecat import (
    SubtitleCatFetcher,
    parse_detail_zh_links,
    parse_search,
)

FIXTURES = Path(__file__).parent / "fixtures" / "subtitlecat"
BASE = "https://www.subtitlecat.com"
SRT = b"1\n00:00:01,000 --> 00:00:02,000\n\xe4\xbd\xa0\xe5\xa5\xbd\n"


def _search() -> str:
    return (FIXTURES / "search_ssis001.html").read_text(encoding="utf-8")


def _detail() -> str:
    return (FIXTURES / "detail_ssis001.html").read_text(encoding="utf-8")


def test_parse_search_filters_code_and_prefers_chinese() -> None:
    hits = parse_search(_search(), "SSIS-001", BASE)
    assert hits, "search fixture drifted"
    assert hits[0].from_chinese
    assert all("ssis001" in h.name.casefold().replace("-", "").replace(" ", "") for h in hits)
    assert hits[0].detail_url.startswith(f"{BASE}/subs/")
    assert parse_search(_search(), "SSIS-00", BASE) == []  # prefix of a longer code
    assert parse_search("<html>nothing</html>", "SSIS-001", BASE) == []


def test_parse_detail_zh_links() -> None:
    links = parse_detail_zh_links(_detail(), BASE)
    assert links["zh-CN"] == f"{BASE}/subs/253/SSIS-001-zh-CN.srt"
    assert links["zh-TW"].endswith("-zh-TW.srt")
    assert parse_detail_zh_links("<a href='x'>no</a>", BASE) == {}


def _fetcher(tmp_path: Path, handler: Callable[[httpx.Request], httpx.Response]) -> SubtitleCatFetcher:
    return SubtitleCatFetcher(
        base_url=BASE,
        min_interval_seconds=0,
        cache_path=tmp_path / "misses.json",
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_fetch_writes_zh_srt(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index.php":
            return httpx.Response(200, text=_search())
        if request.url.path.endswith(".srt"):
            return httpx.Response(200, content=SRT)
        return httpx.Response(200, text=_detail())

    out = tmp_path / "lib"
    dry = _fetcher(tmp_path, handler).fetch("SSIS-001", out, "SSIS-001", dry_run=True)
    assert dry.status == "dry-run" and dry.pick is not None and dry.pick.lang == "zh-CN"
    assert not out.exists()
    result = _fetcher(tmp_path, handler).fetch("SSIS-001", out, "SSIS-001")
    assert result.status == "written"
    assert (out / "SSIS-001.zh.srt").read_bytes() == SRT
    assert _fetcher(tmp_path, handler).fetch("SSIS-001", out, "SSIS-001").status == "exists"


def test_miss_is_cached(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        return httpx.Response(200, text="<table></table>")

    assert _fetcher(tmp_path, handler).fetch("ZZZ-999", tmp_path, "ZZZ-999").status == "miss"
    assert _fetcher(tmp_path, handler).fetch("ZZZ-999", tmp_path, "ZZZ-999").status == "cached-miss"
    assert len(calls) == 1


def test_errors_never_raise(tmp_path: Path) -> None:
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("slow", request=request)

    result = _fetcher(tmp_path, boom).fetch("SSIS-001", tmp_path, "SSIS-001")
    assert result.status == "error" and "ConnectTimeout" in (result.detail or "")

    def html_instead(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".srt"):
            return httpx.Response(200, text="<html>blocked</html>")
        if request.url.path == "/index.php":
            return httpx.Response(200, text=_search())
        return httpx.Response(200, text=_detail())

    assert _fetcher(tmp_path, html_instead).fetch("SSIS-001", tmp_path / "x", "SSIS-001").status == "error"


def test_subtitle_cut_uses_original_strm_filename_and_separate_miss_cache(tmp_path: Path) -> None:
    page = (
        '<tr><td><a href="/wrong-code">XSSIS-001-UC.srt</a></td></tr>'
        '<tr><td><a href="/wrong-serial">SSIS-010-UC.srt</a></td></tr>'
        '<tr><td><a href="/leaked">SSIS-001-leaked.srt</a></td></tr>'
        '<tr><td><a href="/extended">SSIS-001-extended.srt</a></td></tr>'
        '<tr><td><a href="/match">SSIS-1-UC.srt</a></td></tr>'
    )
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path == "/index.php":
            return httpx.Response(200, text=page)
        if request.url.path.endswith(".srt"):
            return httpx.Response(200, content=SRT)
        assert request.url.path == "/match"
        return httpx.Response(200, text=_detail())

    fetcher = _fetcher(tmp_path, handler)
    assert fetcher.fetch("SSIS-001", tmp_path, "SSIS-001").status == "miss"
    (tmp_path / SIDECAR_NAME).write_text(json.dumps({"entries": [{
        "name": "SSIS-001.strm", "file_id": "test", "remote_path": "/remote/SSIS-001-UC.mp4",
    }]}))
    (tmp_path / "SSIS-001.strm").touch()
    result = fetcher.fetch("SSIS-001", tmp_path, "SSIS-001")
    assert result.status == "written"
    assert result.pick is not None and result.pick.name == "SSIS-1-UC.srt"
    assert calls == ["/index.php", "/index.php", "/match", "/subs/253/SSIS-001-zh-CN.srt"]


def test_remaster_code_searches_base_but_only_selects_remaster_subtitle(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index.php":
            assert request.url.params["search"] == "SSIS-001"
            return httpx.Response(200, text=(
                '<tr><td><a href="/standard">SSIS-001.srt</a></td></tr>'
                '<tr><td><a href="/remaster">SSIS-001HD.srt</a></td></tr>'
            ))
        assert request.url.path == "/remaster"
        return httpx.Response(200, text=_detail())

    result = _fetcher(tmp_path, handler).fetch("SSIS-001H", tmp_path, "SSIS-001H", dry_run=True)
    assert result.pick is not None and result.pick.name == "SSIS-001HD.srt"


def test_remaster_subtitle_spellings_do_not_match_standard_release() -> None:
    editions = ["SSIS-001-HD", "SSIS-001_H", "SSIS-001HD.zh-CN", "SSIS-001.AI.1080p"]
    names = ["SSIS-001", "SSIS-010-HD", *editions]
    page = "".join(f'<tr><td><a href="/fixture">{name}.srt</a></td></tr>' for name in names)

    assert [hit.name for hit in parse_search(page, "SSIS-001", BASE)] == ["SSIS-001.srt"]
    remasters = parse_search(page, "SSIS-001", BASE, edition=frozenset({"remastered"}))
    assert {hit.name for hit in remasters} == {f"{name}.srt" for name in editions}


def test_subtitle_timing_warning_preserves_subtitle_and_skips_shared_runtime(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/index.php":
            return httpx.Response(200, text=_search())
        if request.url.path.endswith(".srt"):
            return httpx.Response(200, content=SRT)
        return httpx.Response(200, text=_detail())

    (tmp_path / "SSIS-001.strm").touch()
    (tmp_path / "movie.nfo").write_text("<movie><title>Fixture</title><runtime>90</runtime></movie>")
    result = _fetcher(tmp_path, handler).fetch("SSIS-001", tmp_path, "SSIS-001")
    assert result.status == "written" and result.detail == "subtitle ends at 2s, NFO runtime 5400s (1 cues)"
    assert result.path is not None and result.path.read_bytes() == SRT
    result.path.unlink()
    (tmp_path / "SSIS-001-cd2.strm").touch()
    second = _fetcher(tmp_path, handler).fetch("SSIS-001", tmp_path, "SSIS-001")
    assert second.status == "written" and second.detail is None
