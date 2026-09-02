from pathlib import Path

import httpx
import pytest

from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import ContentFamily, MediaCategory, QueryMode
from shadow_mdc.providers import (
    AirAvProvider,
    AvSoxProvider,
    FanzaProvider,
    Fc2ClubProvider,
    Fc2ContentsProvider,
    Fc2HubProvider,
    FreeJavBtProvider,
    JavLibraryProvider,
    MgstageProvider,
    PaipanconProvider,
    Provider,
    R18DevProvider,
)
from shadow_mdc.providers.base import ProviderError

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def _hints(code: str) -> IdentityHints:
    return IdentityHints(
        term=code,
        mode=QueryMode.CODE,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        code=code,
    )


def _provider(name: str, client: httpx.AsyncClient) -> Provider:
    base_url = "https://fixture.test"
    match name:
        case "airav":
            return AirAvProvider(client, base_url)
        case "avsox":
            return AvSoxProvider(client, base_url)
        case "r18dev":
            return R18DevProvider(client, base_url)
        case "fanza":
            return FanzaProvider(client, base_url)
        case "javlibrary":
            return JavLibraryProvider(client, base_url)
        case "mgstage":
            return MgstageProvider(client, base_url)
        case "fc2club":
            return Fc2ClubProvider(client, base_url)
        case "fc2contents":
            return Fc2ContentsProvider(client, base_url)
        case "fc2hub":
            return Fc2HubProvider(client, base_url)
        case "freejavbt":
            return FreeJavBtProvider(client, base_url)
        case "paipancon":
            return PaipanconProvider(client, base_url)
        case _:
            raise AssertionError(f"unknown provider fixture: {name}")


@pytest.mark.parametrize(
    ("provider_name", "code", "expected_actor"),
    [
        ("r18dev", "IPX-219", "架乃ゆら"),
        ("fanza", "IPX-219", "架乃ゆら"),
        ("javlibrary", "IPX-219", "架乃ゆら"),
        ("mgstage", "ABP-420", "凰かなめ"),
        ("fc2club", "FC2-3131319", "出演者A"),
        ("fc2hub", "FC2-3131319", "販売者A"),
        ("airav", "IPX-219", "架乃ゆら"),
        ("avsox", "IPX-219", "架乃ゆら"),
        ("freejavbt", "FC2-3131319", "出演者A"),
    ],
)
@pytest.mark.asyncio
async def test_additional_provider_parses_offline_fixture(
    provider_name: str,
    code: str,
    expected_actor: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if provider_name == "r18dev":
            name = "r18dev_detail.json"
        elif provider_name == "javlibrary":
            name = "javlibrary_search.html" if request.url.params.get("keyword") else "javlibrary_detail.html"
        elif provider_name == "fc2hub":
            name = "fc2hub_search.html" if request.url.path == "/search" else "fc2hub_detail.html"
        elif provider_name in {"airav", "avsox"}:
            name = (
                f"{provider_name}_search.html"
                if "search" in request.url.path or request.url.params.get("search")
                else f"{provider_name}_detail.html"
            )
        else:
            name = f"{provider_name}_detail.html"
        return httpx.Response(200, text=_fixture(name), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await _provider(provider_name, client).search(_hints(code))

    assert len(records) == 1
    assert records[0].code == code
    assert expected_actor in records[0].actors
    assert records[0].artwork


@pytest.mark.parametrize(
    "provider_name",
    [
        "r18dev",
        "fanza",
        "javlibrary",
        "mgstage",
        "fc2club",
        "fc2contents",
        "fc2hub",
        "paipancon",
        "airav",
        "avsox",
        "freejavbt",
    ],
)
@pytest.mark.asyncio
async def test_additional_provider_returns_empty_for_legal_miss(provider_name: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if provider_name == "r18dev":
            return httpx.Response(200, text="{}", request=request)
        if provider_name in {"fc2club", "paipancon"}:
            return httpx.Response(404, text="not found", request=request)
        if provider_name == "fc2contents":
            return httpx.Response(200, text="<html><body>not found</body></html>", request=request)
        return httpx.Response(200, text="<html><body>not found</body></html>", request=request)

    code = "FC2-9999999" if provider_name.startswith("fc2") or provider_name == "paipancon" else "ZZZZ-999"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _provider(provider_name, client).search(_hints(code)) == []


@pytest.mark.parametrize("provider_name", ["fc2club", "fc2contents", "fc2hub", "paipancon"])
@pytest.mark.asyncio
async def test_fc2_providers_reject_non_fc2_input_without_request(provider_name: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _provider(provider_name, client).search(_hints("IPX-219")) == []


@pytest.mark.parametrize(
    "provider_name",
    [
        "r18dev",
        "fanza",
        "javlibrary",
        "mgstage",
        "fc2club",
        "fc2contents",
        "fc2hub",
        "paipancon",
        "airav",
        "avsox",
        "freejavbt",
    ],
)
@pytest.mark.asyncio
async def test_additional_provider_rejects_malformed_code_without_request(
    provider_name: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await _provider(provider_name, client).search(_hints("not-a-code")) == []


@pytest.mark.parametrize(
    "provider_name",
    [
        "r18dev",
        "fanza",
        "javlibrary",
        "mgstage",
        "fc2club",
        "fc2contents",
        "fc2hub",
        "paipancon",
        "airav",
        "avsox",
        "freejavbt",
    ],
)
@pytest.mark.asyncio
async def test_additional_provider_reports_structure_drift(provider_name: str) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if provider_name == "r18dev":
            return httpx.Response(
                200,
                json={"content_id": "ipx00219", "dvd_id": "IPX-219"},
                request=request,
            )
        if provider_name == "javlibrary" and request.url.params.get("keyword"):
            return httpx.Response(200, text=_fixture("javlibrary_search.html"), request=request)
        if provider_name == "fc2hub" and request.url.path == "/search":
            return httpx.Response(200, text=_fixture("fc2hub_search.html"), request=request)
        if provider_name == "airav" and request.url.params.get("search"):
            return httpx.Response(200, text=_fixture("airav_search.html"), request=request)
        if provider_name == "avsox" and "/search/" in request.url.path:
            return httpx.Response(200, text=_fixture("avsox_search.html"), request=request)
        drift_markers = {
            "fanza": '<div class="hreview"></div>',
            "javlibrary": '<div id="video_info"></div>',
            "mgstage": '<div class="detail_left"></div>',
            "fc2club": '<img class="responsive" src="/cover.jpg">',
            "fc2contents": '<div class="items_article_headerInfo"></div>',
            "fc2hub": "<html><body></body></html>",
            "paipancon": "<html><body><h2></h2><title>FC2-PPV-3131319</title></body></html>",
            "airav": "<html><body></body></html>",
            "avsox": "<html><body></body></html>",
            "freejavbt": '<div class="single-video-info"></div>',
        }
        return httpx.Response(200, text=drift_markers[provider_name], request=request)

    code = "FC2-3131319" if provider_name.startswith("fc2") or provider_name == "paipancon" else "IPX-219"
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="title missing"):
            await _provider(provider_name, client).search(_hints(code))


@pytest.mark.asyncio
async def test_fc2contents_provider_parses_offline_fixture() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture("fc2contents_detail.html"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await _provider("fc2contents", client).search(_hints("FC2-3131319"))

    assert len(records) == 1
    record = records[0]
    assert record.code == "FC2-3131319"
    assert record.title == "限定配信スペシャル"
    assert record.studio == "販売者A"
    assert record.release_date is not None
    assert record.runtime_seconds == 996
    assert "個人撮影" in record.tags
    assert record.artwork
    assert record.plot


@pytest.mark.asyncio
async def test_paipancon_provider_parses_offline_fixture() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture("paipancon_detail.html"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await _provider("paipancon", client).search(_hints("FC2-3131319"))

    assert len(records) == 1
    record = records[0]
    assert record.code == "FC2-3131319"
    assert record.title == "限定配信スペシャル"
    assert any("cover.jpg" in str(item.url) for item in record.artwork)
