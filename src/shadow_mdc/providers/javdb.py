import asyncio
from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from ..media.magnets import MagnetLink, parse_magnet_links_from_html
from .html import first_text, image_artwork, link_texts, meta_content, parse_date, parse_runtime_seconds


class JavDBProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="javdb",
            name="JavDB",
            query_modes=frozenset({QueryMode.CODE, QueryMode.TEXT, QueryMode.URL}),
            families=frozenset({ContentFamily.JAV, ContentFamily.CHINESE, ContentFamily.ANIMATION}),
        )

    async def fetch_html(self, url: str, *, params: dict[str, str] | None = None) -> str:
        return await self._get_text(self.descriptor.id, url, params=params)

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        if hints.mode is QueryMode.URL and hints.source_url:
            return [await self._detail(hints.source_url)]
        url = f"{self._base_url}/search?q={quote(hints.term)}&f=all"
        html = await self._get_text(self.descriptor.id, url, params={"locale": "zh"})
        root = HTMLParser(html)
        links: list[str] = []
        for selector in (".movie-list .item a", "a.box", ".grid-item a"):
            for node in root.css(selector):
                href = node.attributes.get("href")
                if href and "/v/" in href:
                    absolute = urljoin(self._base_url + "/", href)
                    if absolute not in links:
                        links.append(absolute)
        canonical = root.css_first('link[rel="canonical"]')
        if not links and canonical is not None:
            href = canonical.attributes.get("href")
            if href and "/v/" in href:
                links.append(urljoin(self._base_url + "/", href))
        results = await asyncio.gather(*(self._detail(link) for link in links[:5]), return_exceptions=True)
        records = [result for result in results if isinstance(result, ProviderRecord)]
        if links and not records:
            failure = next((result for result in results if isinstance(result, Exception)), None)
            if failure is not None:
                raise failure
        return records

    async def _detail(self, url: str) -> ProviderRecord:
        html = await self._get_text(self.descriptor.id, url, params={"locale": "zh"})
        root = HTMLParser(html)
        title = first_text(root, ("h2.title strong", "h2.title", "h1.title")) or meta_content(
            root, "og:title"
        )
        if title is None:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        full_text = root.text(separator=" ", strip=True)
        code = first_text(root, (".video-meta-panel strong", ".movie-panel-info strong"))
        parsed_code, family = extract_code(code or title)
        external_id = urlparse(url).path.rstrip("/").split("/")[-1]
        studio = first_text(root, ('a[href*="/makers/"]', 'a[href*="/publishers/"]'))
        plot = meta_content(root, "description")
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
            plot=plot,
            actors=link_texts(root, ('a[href*="/actors/"]', 'a[href*="/performers/"]')),
            tags=link_texts(root, ('a[href*="/tags/"]',)),
            artwork=image_artwork(root, url),
            language="zh",
        )

    async def magnets(self, external_id_or_url: str) -> tuple[MagnetLink, ...]:
        """List magnet URIs from the public detail page (display/save only)."""

        if external_id_or_url.startswith("http://") or external_id_or_url.startswith("https://"):
            url = external_id_or_url
        else:
            url = f"{self._base_url}/v/{external_id_or_url}"
        html = await self._get_text(self.descriptor.id, url, params={"locale": "zh"})
        return parse_magnet_links_from_html(html, provider=self.descriptor.id)
