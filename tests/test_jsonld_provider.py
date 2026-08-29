import httpx
import pytest

from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import QueryMode
from shadow_mdc.providers.base import ProviderError
from shadow_mdc.providers.jsonld import JsonLdProvider


@pytest.mark.asyncio
async def test_jsonld_movie_is_normalized() -> None:
    html = """
    <script type="application/ld+json">
    {
      "@context": "https://schema.org",
      "@type": "VideoObject",
      "@id": "scene-42",
      "name": "Long-tail scene",
      "datePublished": "2025-03-04",
      "duration": "PT1H2M3S",
      "actor": [{"name": "Alice"}, {"name": "Bob"}],
      "publisher": {"name": "Fixture Studio"},
      "keywords": "tag one, tag two",
      "image": "https://example.test/cover.jpg"
    }
    </script>
    """

    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=html, request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await JsonLdProvider(client).search(
            IdentityHints(
                term="https://example.test/scene",
                mode=QueryMode.URL,
                source_url="https://example.test/scene",
            )
        )

    item = records[0]
    assert item.external_id == "scene-42"
    assert item.title == "Long-tail scene"
    assert item.runtime_seconds == 3723
    assert item.actors == ("Alice", "Bob")
    assert str(item.artwork[0].url) == "https://example.test/cover.jpg"


@pytest.mark.asyncio
async def test_jsonld_reports_unsupported_page() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text="<html><title>none</title></html>", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError, match="no supported JSON-LD object"):
            await JsonLdProvider(client).search(
                IdentityHints(
                    term="https://example.test/empty",
                    mode=QueryMode.URL,
                    source_url="https://example.test/empty",
                )
            )
