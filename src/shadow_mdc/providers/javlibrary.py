import asyncio
from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, link_texts, parse_date, parse_runtime_seconds
from .html_fields import first_image_artwork


class JavLibraryProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="javlibrary",
            name="JavLibrary",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV or requested_code.startswith("FC2-"):
            return []
        search_url = f"{self._base_url}/ja/vl_searchbyid.php?keyword={quote(requested_code)}"
        html = await self._get_text(self.descriptor.id, search_url)
        root = HTMLParser(html)
        if root.css_first("#video_info") is not None:
            return [self._parse_detail(html, search_url, requested_code)]
        links: list[str] = []
        for item in root.css(".video[id]"):
            item_code, _ = extract_code(first_text(item, (".id",)) or "")
            link = item.css_first("a[href]")
            href = link.attributes.get("href") if link is not None else None
            if href and item_code == requested_code:
                links.append(urljoin(search_url, href))
        if not links:
            return []
        results = await asyncio.gather(
            *(self._detail(url, requested_code) for url in links[:3]),
            return_exceptions=True,
        )
        records = [item for item in results if isinstance(item, ProviderRecord)]
        if not records:
            failure = next((item for item in results if isinstance(item, Exception)), None)
            if failure is not None:
                raise failure
        return records

    async def _detail(self, url: str, requested: str) -> ProviderRecord:
        html = await self._get_text(self.descriptor.id, url)
        return self._parse_detail(html, url, requested)

    def _parse_detail(self, html: str, url: str, requested: str) -> ProviderRecord:
        root = HTMLParser(html)
        info = root.css_first("#video_info")
        if info is None:
            raise ProviderError(self.descriptor.id, "parse", "detail metadata missing")
        raw_code = first_text(root, ("#video_id .text",)) or requested
        code, family = extract_code(raw_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        raw_title = first_text(root, ("h3 a", "h3", "h1"))
        if not raw_title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        title = raw_title.removeprefix(raw_code).strip() or raw_title
        full_text = root.text(separator=" ", strip=True)
        external_id = urlparse(url).query or urlparse(url).path
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=external_id,
            source_url=url,
            code=code,
            title=title,
            original_title=title,
            family=ContentFamily.JAV,
            release_date=parse_date(first_text(root, ("#video_date .text",)) or full_text),
            runtime_seconds=parse_runtime_seconds(first_text(root, ("#video_length .text",)) or full_text),
            studio=first_text(root, (".maker a",)),
            label=first_text(root, (".label a",)),
            actors=link_texts(root, (".star a",)),
            directors=link_texts(root, (".director a",)),
            tags=link_texts(root, (".genre a",)),
            artwork=first_image_artwork(root, self._base_url, ("#video_jacket_img",)),
            language="ja",
        )
