import asyncio
from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, image_artwork, link_texts, meta_content, parse_date, parse_runtime_seconds


class JavBusProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str):
        super().__init__(client)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="javbus",
            name="JavBus",
            query_modes=frozenset({QueryMode.CODE, QueryMode.TEXT}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        url = f"{self._base_url}/search/{quote(hints.term)}"
        html = await self._get_text(self.descriptor.id, url)
        root = HTMLParser(html)
        links: list[str] = []
        for node in root.css("a.movie-box"):
            href = node.attributes.get("href")
            absolute = urljoin(self._base_url + "/", href) if href else None
            if absolute and absolute not in links:
                links.append(absolute)
        results = await asyncio.gather(*(self._detail(link) for link in links[:5]), return_exceptions=True)
        records = [result for result in results if isinstance(result, ProviderRecord)]
        if links and not records:
            failure = next((result for result in results if isinstance(result, Exception)), None)
            if failure is not None:
                raise failure
        return records

    async def _detail(self, url: str) -> ProviderRecord:
        html = await self._get_text(self.descriptor.id, url)
        root = HTMLParser(html)
        title = first_text(root, ("h3", "h1")) or meta_content(root, "og:title")
        if title is None:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        full_text = root.text(separator=" ", strip=True)
        parsed_code, family = extract_code(title)
        external_id = urlparse(url).path.rstrip("/").split("/")[-1]
        studio = first_text(root, ('a[href*="/studio/"]', 'a[href*="/label/"]'))
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=external_id,
            source_url=url,
            code=parsed_code,
            title=title,
            family=family,
            release_date=parse_date(full_text),
            runtime_seconds=parse_runtime_seconds(full_text),
            studio=studio,
            actors=link_texts(root, ('a[href*="/star/"]',)),
            tags=link_texts(root, ('a[href*="/genre/"]',)),
            artwork=image_artwork(root, url),
            language="zh",
        )
