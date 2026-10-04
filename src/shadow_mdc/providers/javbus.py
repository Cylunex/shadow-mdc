import asyncio
from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, image_artwork, link_texts, meta_content, parse_date, parse_runtime_seconds
from .html_fields import sample_image_artwork

# existmag=all lists works without magnets too; the default only shows magnet-backed ones.
_JAVBUS_HEADERS = {"Cookie": "existmag=all"}


def _panel_code(panel: Node | HTMLParser) -> str | None:
    """Return the 識別碼 value from the info panel, if present."""

    for paragraph in panel.css("p"):
        header = paragraph.css_first("span.header")
        label = header.text(strip=True) if header is not None else ""
        if header is None or not ("識別碼" in label or "识别码" in label):
            continue
        spans = [node.text(strip=True) for node in paragraph.css("span")]
        value = next((text for text in spans if text and text != label), None)
        return value or paragraph.text(strip=True).split(":")[-1].strip() or None
    return None


def _panel_actors(root: HTMLParser, panel: Node | HTMLParser) -> tuple[str, ...]:
    """Actors from the star list; JavBus only lists female performers there."""

    for scope, selectors in (
        (root, ("#star-div .star-name a", "#avatar-waterfall .star-name a")),
        (panel, (".star-name a", 'span.genre a[href*="/star/"]', 'a[href*="/star/"]')),
    ):
        names = link_texts(scope, selectors)
        if names:
            return names
    return ()


class JavBusProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
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
        links: list[str] = []
        # Censored search first, then the uncensored index (javinizer-go javbus scraper).
        for path in ("search", "uncensored/search"):
            url = f"{self._base_url}/{path}/{quote(hints.term)}"
            html = await self._get_text(self.descriptor.id, url, headers=_JAVBUS_HEADERS)
            root = HTMLParser(html)
            for node in root.css("a.movie-box"):
                href = node.attributes.get("href")
                absolute = urljoin(self._base_url + "/", href) if href else None
                if absolute and absolute not in links:
                    links.append(absolute)
            if links:
                break
        results = await asyncio.gather(*(self._detail(link) for link in links[:5]), return_exceptions=True)
        records = [result for result in results if isinstance(result, ProviderRecord)]
        if links and not records:
            failure = next((result for result in results if isinstance(result, Exception)), None)
            if failure is not None:
                raise failure
        return records

    async def _detail(self, url: str) -> ProviderRecord:
        html = await self._get_text(self.descriptor.id, url, headers=_JAVBUS_HEADERS)
        root = HTMLParser(html)
        title = first_text(root, ("div.container > h3", "h3", "h1")) or meta_content(root, "og:title")
        if title is None:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        # Scope field parsing to the movie info panel so related-movie cards, sidebars
        # and nav menus cannot leak dates, studios or "actors" (javinizer-go javbus).
        panel = root.css_first("div.movie div.info") or root.css_first("div.info") or root
        full_text = panel.text(separator=" ", strip=True)
        parsed_code, family = extract_code(_panel_code(panel) or title)
        external_id = urlparse(url).path.rstrip("/").split("/")[-1]
        studio = first_text(panel, ('a[href*="/studio/"]', 'a[href*="/label/"]'))
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
            actors=_panel_actors(root, panel),
            tags=link_texts(panel, ('a[href*="/genre/"]',)),
            artwork=tuple(
                (
                    *image_artwork(root, url),
                    *sample_image_artwork(
                        root,
                        url,
                        (
                            "#sample-waterfall a.sample-box",
                            "#sample-waterfall a.sample-box img",
                            ".sample-waterfall a",
                            "#sample-waterfall a",
                            "a.sample-box",
                        ),
                        limit=12,
                    ),
                )
            ),
            language="zh",
        )
