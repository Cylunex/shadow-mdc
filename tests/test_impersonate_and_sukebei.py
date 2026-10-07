from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shadow_mdc.providers import base as base_module
from shadow_mdc.providers.base import HttpProvider, ProviderError
from shadow_mdc.providers.impersonate import ImpersonatedResponse, ImpersonateError
from shadow_mdc.providers.sukebei import (
    SukebeiClient,
    code_pattern,
    extract_screenshots,
    parse_search_html,
    query_variants,
    rows_to_magnets,
)
from shadow_mdc.services import daily_hot_seed

FIXTURES = Path(__file__).parent / "fixtures" / "sukebei"


def _client(status: int, body: str = "denied") -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(status, text=body)))


class _Probe(HttpProvider):
    async def get(self, url: str) -> str:
        return await self._get_text("probe", url, headers={"Cookie": "a=1"})


@pytest.mark.asyncio
async def test_blocked_get_falls_back_to_impersonated_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict[str, object] = {}

    async def fake_get(url: str, **kwargs: object) -> ImpersonatedResponse:
        seen["url"] = url
        seen["headers"] = kwargs["headers"]
        return ImpersonatedResponse(200, url, "<html>real page</html>")

    monkeypatch.setattr(base_module, "impersonated_get", fake_get)
    async with _client(403) as client:
        assert await _Probe(client, retries=0).get("https://example.test/x") == "<html>real page</html>"
    assert seen["url"] == "https://example.test/x"
    assert dict(seen["headers"])["Cookie"] == "a=1"  # type: ignore[call-overload]


@pytest.mark.asyncio
async def test_impersonated_challenge_or_error_stays_blocked(monkeypatch: pytest.MonkeyPatch) -> None:
    async def challenge(url: str, **kwargs: object) -> ImpersonatedResponse:
        return ImpersonatedResponse(403, url, "<title>Just a moment...</title> cf-browser-verification")

    monkeypatch.setattr(base_module, "impersonated_get", challenge)
    async with _client(403) as client:
        with pytest.raises(ProviderError) as info:
            await _Probe(client, retries=0).get("https://example.test/x")
    assert info.value.reason == "blocked" and "impersonate: Cloudflare" in info.value.detail

    async def boom(url: str, **kwargs: object) -> ImpersonatedResponse:
        raise ImpersonateError("DNSError")

    monkeypatch.setattr(base_module, "impersonated_get", boom)
    async with _client(451) as client:
        with pytest.raises(ProviderError) as info:
            await _Probe(client, retries=0).get("https://example.test/x")
    assert info.value.reason == "blocked" and "DNSError" in info.value.detail


@pytest.mark.asyncio
async def test_non_blocked_errors_do_not_use_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    async def never(url: str, **kwargs: object) -> ImpersonatedResponse:
        raise AssertionError("fallback must not run")

    monkeypatch.setattr(base_module, "impersonated_get", never)
    async with _client(404) as client:
        with pytest.raises(ProviderError) as info:
            await _Probe(client, retries=0).get("https://example.test/x")
    assert info.value.reason == "http"


@pytest.mark.asyncio
async def test_daily_hot_fetch_falls_back_on_403(monkeypatch: pytest.MonkeyPatch) -> None:
    async def fake_get(url: str, **kwargs: object) -> ImpersonatedResponse:
        return ImpersonatedResponse(200, url, '{"data": {"children": []}}')

    monkeypatch.setattr(daily_hot_seed, "impersonated_get", fake_get)
    async with _client(403) as client:
        assert "children" in await daily_hot_seed.default_fetch_text(client, "https://r.test/hot.json")
    async with _client(200, "ok") as client:
        assert await daily_hot_seed.default_fetch_text(client, "https://r.test/a") == "ok"


def test_query_variants_and_code_pattern() -> None:
    assert query_variants("SSIS-001") == ["SSIS-001", "SSIS001"]
    assert query_variants("ABP-0123") == ["ABP-0123", "ABP0123", "ABP-123"]
    pattern = code_pattern("SSIS-001")
    assert pattern.search("+++ [HD] SSIS-001 title")
    assert pattern.search("ssis00001 leak")
    assert pattern.search("SSIS_1.mp4")
    assert not pattern.search("SSIS-0011 other")
    assert not pattern.search("XSSIS-001")


def test_parse_search_fixture_to_magnets() -> None:
    rows = parse_search_html((FIXTURES / "search_ssis001.html").read_text(encoding="utf-8"))
    assert rows, "parser drift: no rows"
    first = rows[0]
    assert first.view_url.startswith("https://sukebei.nyaa.si/view/")
    assert first.magnet.startswith("magnet:?xt=urn:btih:") and "&amp;" not in first.magnet
    assert first.size_bytes and first.size_bytes > 1_000_000_000
    assert first.timestamp and first.timestamp > 1_500_000_000
    magnets = rows_to_magnets(rows, "SSIS-001")
    assert magnets and all(m.provider == "sukebei" for m in magnets)
    assert len({m.info_hash for m in magnets}) == len(magnets)
    assert rows_to_magnets(rows, "ABP-999") == ()


def test_parse_invalid_html_returns_nothing() -> None:
    assert parse_search_html("") == []
    assert parse_search_html("<table class='torrent-list'><tbody><tr><td>x</td></tr></tbody></table>") == []


def test_extract_screenshots_from_description() -> None:
    shots = extract_screenshots((FIXTURES / "view_desc.html").read_text(encoding="utf-8"))
    assert shots == ("https://img.example.com/a/ssis001_s.jpg",)
    assert extract_screenshots("<html></html>") == ()


@pytest.mark.asyncio
async def test_sukebei_client_tries_variants() -> None:
    page = (FIXTURES / "search_ssis001.html").read_text(encoding="utf-8")
    queries: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        queries.append(request.url.params["q"])
        return httpx.Response(200, text=page if request.url.params["q"] == "SSIS001" else "<html></html>")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        magnets = await SukebeiClient(client, retries=0).magnets("SSIS-001")
    assert queries == ["SSIS-001", "SSIS001"]
    assert magnets
