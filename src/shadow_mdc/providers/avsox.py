from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, link_texts, parse_date, parse_runtime_seconds
from .html_fields import first_image_artwork


class AvSoxProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="avsox",
            name="AVSOX",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV:
            return []
        search_url = f"{self._base_url}/cn/search/{quote(requested_code)}"
        search_html = await self._get_text(self.descriptor.id, search_url)
        search_root = HTMLParser(search_html)
        detail_url: str | None = None
        for item in search_root.css("#waterfall > div, #waterfall .item"):
            item_code, _ = extract_code(first_text(item, ("date",)) or "")
            link = item.css_first("a[href]")
            href = link.attributes.get("href") if link is not None else None
            if href and item_code == requested_code:
                detail_url = urljoin(search_url, href)
                break
        if detail_url is None:
            return []
        html = await self._get_text(self.descriptor.id, detail_url)
        root = HTMLParser(html)
        raw_title = first_text(root, (".container h3", "h3"))
        if not raw_title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        raw_code = first_text(root, (".info p span",)) or requested_code
        code, family = extract_code(raw_code)
        title = raw_title.removeprefix(raw_code).strip() or raw_title
        full_text = root.text(separator=" ", strip=True)
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=urlparse(detail_url).path.rstrip("/").split("/")[-1],
                source_url=detail_url,
                code=code,
                title=title,
                original_title=title,
                family=family,
                release_date=parse_date(full_text),
                runtime_seconds=parse_runtime_seconds(full_text),
                studio=first_text(root, ('a[href*="/studio/"]',)),
                series=first_text(root, ('a[href*="/series/"]',)),
                actors=link_texts(root, ("#avatar-waterfall a span",)),
                tags=link_texts(root, (".genre a",)),
                artwork=first_image_artwork(root, self._base_url, ("a.bigImage",)),
                language="zh",
            )
        ]
