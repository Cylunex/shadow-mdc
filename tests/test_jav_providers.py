from pathlib import Path

import httpx
import pytest

from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderError
from shadow_mdc.providers.javbus import JavBusProvider
from shadow_mdc.providers.javdb import JavDBProvider

FIXTURES = Path(__file__).parent / "fixtures"


def _read(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.parametrize(
    ("provider_name", "search_fixture", "detail_fixture", "expected_title", "expected_actor"),
    [
        ("javdb", "javdb_search.html", "javdb_detail.html", "SSIS-123 Fixture title", "Alice"),
        ("javbus", "javbus_search.html", "javbus_detail.html", "ABP-123 Fixture Bus title", "Bob"),
    ],
)
@pytest.mark.asyncio
async def test_jav_provider_fixture(
    provider_name: str,
    search_fixture: str,
    detail_fixture: str,
    expected_title: str,
    expected_actor: str,
) -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        fixture = search_fixture if "search" in request.url.path else detail_fixture
        return httpx.Response(200, text=_read(fixture), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = (
            JavDBProvider(client, "https://fixture.test")
            if provider_name == "javdb"
            else JavBusProvider(client, "https://fixture.test")
        )
        records = await provider.search(
            IdentityHints(
                term="SSIS-123",
                mode=QueryMode.CODE,
                family=ContentFamily.JAV,
                code="SSIS-123",
            )
        )

    assert len(records) == 1
    assert records[0].title == expected_title
    assert records[0].actors == (expected_actor,)
    assert records[0].runtime_seconds is not None
    assert str(records[0].artwork[0].url).startswith("https://fixture.test/")


@pytest.mark.asyncio
async def test_detail_structure_drift_is_reported() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        html = _read("javdb_search.html") if "search" in request.url.path else "<html></html>"
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JavDBProvider(client, "https://fixture.test")
        with pytest.raises(ProviderError, match="detail title missing"):
            await provider.search(
                IdentityHints(
                    term="SSIS-123",
                    mode=QueryMode.CODE,
                    family=ContentFamily.JAV,
                    code="SSIS-123",
                )
            )
