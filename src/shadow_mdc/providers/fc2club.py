from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text
from .html_fields import field_links, first_image_artwork


class Fc2ClubProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fc2club",
            name="FC2Club",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        code, family = extract_code(requested)
        if code is None or family is not ContentFamily.JAV or not code.startswith("FC2-"):
            return []
        number = code.removeprefix("FC2-")
        url = f"{self._base_url}/html/FC2-{quote(number)}.html"
        try:
            html = await self._get_text(self.descriptor.id, url)
        except ProviderError as exc:
            if exc.reason == "http" and "status=404" in exc.detail:
                return []
            raise
        root = HTMLParser(html)
        title = first_text(root, ("h3",))
        if not title:
            if root.css_first(".responsive") is None:
                return []
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        title = title.removeprefix(f"FC2-{number}").strip() or title
        studio = next(iter(field_links(root, ("卖家信息", "販売者"))), None)
        actors = field_links(root, ("女优名字", "女優"))
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=number,
                source_url=url,
                code=code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                studio=studio,
                series="FC2",
                actors=actors,
                tags=field_links(root, ("影片标签", "タグ")),
                artwork=first_image_artwork(root, self._base_url, ("img.responsive",)),
                language="zh",
            )
        ]
