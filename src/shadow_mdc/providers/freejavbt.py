import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, link_texts, parse_date, parse_runtime_seconds
from .html_fields import first_image_artwork


class FreeJavBtProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="freejavbt",
            name="FreeJavBT metadata",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV:
            return []
        url = f"{self._base_url}/{quote(requested_code)}"
        try:
            html = await self._get_text(self.descriptor.id, url)
        except ProviderError as exc:
            if exc.reason == "http" and "status=404" in exc.detail:
                return []
            raise
        root = HTMLParser(html)
        if root.css_first(".single-video-info") is None:
            return []
        raw_title = first_text(root, ("title", "h1"))
        if not raw_title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        title = re.sub(r"(?i)\|?\s*FREE JAV BT\s*$", "", raw_title)
        title = re.sub(re.escape(requested_code), "", title, flags=re.IGNORECASE).strip(" |-_")
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        full_text = root.text(separator=" ", strip=True)
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=requested_code,
                source_url=url,
                code=requested_code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                release_date=parse_date(full_text),
                runtime_seconds=parse_runtime_seconds(full_text),
                studio=first_text(root, ('a[href*="maker"]', 'a[href*="studio"]')),
                series=first_text(root, ('a[href*="series"]',)),
                actors=link_texts(root, ("a.actress",)),
                tags=link_texts(root, ("a.genre",)),
                artwork=first_image_artwork(
                    root,
                    self._base_url,
                    (
                        "img.video-cover",
                        "img.col-lg-2.col-md-2.col-sm-6.col-12",
                    ),
                ),
                language="zh",
            )
        ]
