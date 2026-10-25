"""SubtitleCat zh subtitle lookup: offline fixtures, drift, misses, never-raise."""

from __future__ import annotations

from pathlib import Path

import httpx

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


def _fetcher(tmp_path: Path, handler) -> SubtitleCatFetcher:
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
