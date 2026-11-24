import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import QueryMode
from shadow_mdc.providers import OnePondoProvider, ProviderRegistry

_BASE = "https://catalog.example.com"
_CODE = "1PONDO-012326-123"
_FIXTURE = Path(__file__).parent / "fixtures" / "onepondo_movie.json"


def _hints(code: str) -> IdentityHints:
    return IdentityHints(term=code, code=code, mode=QueryMode.CODE)


async def test_onepondo_maps_verified_first_party_fields() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/dyn/phpauto/movie_details/movie_id/012326_123.json"
        return httpx.Response(200, text=_FIXTURE.read_text(encoding="utf-8"))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        batch = await ProviderRegistry([OnePondoProvider(client, _BASE)]).search(_hints(_CODE))
    assert not batch.failures
    (record,) = batch.records
    assert (record.code, record.external_id, record.title) == (_CODE, "012326_123", "テスト作品")
    assert (record.release_date, record.runtime_seconds) == (date(2026, 1, 23), 3689)
    assert (record.studio, record.actors, record.tags) == ("一本道", ("俳優名",), ("テストタグ",))
    assert str(record.artwork[0].url) == "https://images.example.com/poster.jpg"


@pytest.mark.parametrize("code", ["012326-123", "CARIB-012326-123", "1PONDO-012326-12", "1PONDO-012326-1234"])
async def test_onepondo_never_queries_bare_or_foreign_codes(code: str) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        pytest.fail(f"unexpected request for {code}")

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        assert await OnePondoProvider(client, _BASE).search(_hints(code)) == []


@pytest.mark.parametrize(("field", "value"), [("MovieID", "012326_124"), ("SiteID", 2476)])
async def test_onepondo_rejects_wrong_movie_and_site(field: str, value: str | int) -> None:
    data = json.loads(_FIXTURE.read_text(encoding="utf-8"))
    data[field] = value
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json=data)),
    ) as client:
        assert await OnePondoProvider(client, _BASE).search(_hints(_CODE)) == []


async def test_onepondo_reports_schema_drift_through_registry() -> None:
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"title": "unexpected"})),
    ) as client:
        batch = await ProviderRegistry([OnePondoProvider(client, _BASE)]).search(_hints(_CODE))
    assert not batch.records
    (failure,) = batch.failures
    assert (failure.provider, failure.reason) == ("onepondo", "parse")
