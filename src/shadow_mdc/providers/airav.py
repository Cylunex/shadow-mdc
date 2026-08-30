from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, link_texts, parse_date
from .html_fields import first_image_artwork


class AirAvProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="airav",
            name="AirAV",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV:
            return []
        search_url = f"{self._base_url}/?search={quote(requested_code)}"
        search_html = await self._get_text(self.descriptor.id, search_url)
        search_root = HTMLParser(search_html)
        detail_url: str | None = None
        for card in search_root.css(".coverImageBox"):
            image = card.css_first("img[alt]")
            card_code, _ = extract_code((image.attributes.get("alt") or "") if image is not None else "")
            link = card.css_first("a[href]")
            href = link.attributes.get("href") if link is not None else None
            if href and card_code == requested_code:
                detail_url = urljoin(search_url, href)
                break
        if detail_url is None:
            return []
        html = await self._get_text(self.descriptor.id, detail_url)
        root = HTMLParser(html)
        raw_code = first_text(root, ("h5.text-primary",)) or requested_code
        code, family = extract_code(raw_code)
        headings = [node.text(separator=" ", strip=True) for node in root.css("h5.d-none.d-md-block")]
        title = next((value for value in headings if extract_code(value)[0] is None), None)
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
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
                studio=first_text(root, ('a[href*="video_factory"]',)),
                plot=first_text(root, (".synopsis p", ".synopsis")),
                actors=link_texts(root, (".videoAvstarListItem a",)),
                tags=link_texts(root, (".tagBtnMargin a",)),
                artwork=first_image_artwork(
                    root,
                    self._base_url,
                    (".videoPlayerMobile img",),
                ),
                language="zh",
            )
        ]
