from pathlib import Path

import httpx
import pytest

from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import ContentFamily, MediaCategory, QueryMode
from shadow_mdc.providers.base import ProviderError
from shadow_mdc.providers.jav321 import Jav321Provider

FIXTURE = Path(__file__).parent / "fixtures" / "jav321_detail.html"


def _hints(code: str = "SONE-118") -> IdentityHints:
    return IdentityHints(
        term=code,
        mode=QueryMode.CODE,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        code=code,
    )


@pytest.mark.asyncio
async def test_jav321_parses_exact_code_metadata() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        assert request.method == "POST"
        assert request.url.path == "/search"
        assert request.content == b"sn=SONE-118"
        return httpx.Response(200, text=FIXTURE.read_text(encoding="utf-8"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await Jav321Provider(client, "https://www.jav321.com").search(_hints())

    assert len(records) == 1
    record = records[0]
    assert record.code == "SONE-118"
    assert record.title == "世界最高峰の愛人"
    assert record.actors == ("河北彩花",)
    assert record.studio == "エスワンナンバーワンスタイル"
    assert record.release_date is not None and record.release_date.isoformat() == "2024-03-26"
    assert record.runtime_seconds == 179 * 60
    assert record.tags == ("ドラマ",)
    assert record.plot == "身の周りの世話から全てをご奉仕してくれる、最高の三日間を描いた作品です。"
    assert [item.kind for item in record.artwork] == ["fanart", "thumb"]
    assert all(str(item.url).startswith("https://www.jav321.com/") for item in record.artwork)


@pytest.mark.asyncio
async def test_jav321_returns_no_candidate_for_legal_miss() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="AVが見つかりませんでした", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await Jav321Provider(client, "https://www.jav321.com").search(_hints("ZZZZ-999")) == []


@pytest.mark.asyncio
async def test_jav321_ignores_embedded_player_script_before_plot() -> None:
    html = FIXTURE.read_text(encoding="utf-8").replace(
        '<div class="row"><div class="col-md-12"></div></div>',
        '<div class="row"><div class="col-md-12">'
        "(adsbyjuicy = window.adsbyjuicy || []).push({'adzone': 1}); "
        "videojs('sample').on('pause', function () { $('#overlay').show(); });"
        "</div></div>",
    )

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await Jav321Provider(client, "https://www.jav321.com").search(_hints())

    assert records[0].plot == "身の周りの世話から全てをご奉仕してくれる、最高の三日間を描いた作品です。"


@pytest.mark.asyncio
async def test_jav321_rejects_invalid_or_drifted_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><h1>changed</h1></html>", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="heading missing"):
            await Jav321Provider(client, "https://www.jav321.com").search(_hints("not-a-code"))
