from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, link_texts, meta_content
from .html_fields import first_image_artwork


class Fc2HubProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fc2hub",
            name="FC2Hub",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        code, family = extract_code(requested)
        if code is None or family is not ContentFamily.JAV or not code.startswith("FC2-"):
            return []
        number = code.removeprefix("FC2-")
        search_url = f"{self._base_url}/search?kw={quote(number)}"
        search_html = await self._get_text(self.descriptor.id, search_url)
        search_root = HTMLParser(search_html)
        detail_url = _detail_url(search_root, search_url, number)
        if detail_url is None:
            return []
        html = await self._get_text(self.descriptor.id, detail_url)
        root = HTMLParser(html)
        headings = [node.text(separator=" ", strip=True) for node in root.css("h1")]
        title = next((value for value in headings if number not in value), None) or meta_content(
            root, "og:title"
        )
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        studio = first_text(root, (".col-8", ".seller"))
        plot = first_text(root, (".col.des", ".description"))
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=urlparse(detail_url).path.rstrip("/").split("/")[-1],
                source_url=detail_url,
                code=code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                studio=studio,
                series="FC2",
                plot=plot,
                actors=(studio,) if studio else (),
                tags=link_texts(root, ('.card-text a[href*="/tag/"]',)),
                artwork=first_image_artwork(
                    root,
                    self._base_url,
                    ('a[data-fancybox="gallery"]',),
                ),
                language="ja",
            )
        ]


def _detail_url(root: HTMLParser, search_url: str, number: str) -> str | None:
    for selector in (f'link[href*="id{number}"]', f'a[href*="id{number}"]'):
        node = root.css_first(selector)
        href = node.attributes.get("href") if node is not None else None
        if href:
            return urljoin(search_url, href)
    return None
