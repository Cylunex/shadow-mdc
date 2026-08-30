from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, parse_date
from .html_fields import field_links, field_text, first_image_artwork, integer_minutes


class MgstageProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="mgstage",
            name="MGStage",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV or requested_code.startswith("FC2-"):
            return []
        url = f"{self._base_url}/product/product_detail/{quote(requested_code)}/"
        html = await self._get_text(self.descriptor.id, url, headers={"Cookie": "adc=1"})
        root = HTMLParser(html)
        detail = root.css_first(".detail_left")
        if detail is None:
            return []
        title = first_text(root, (".common_detail_cover h1", "h1"))
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        raw_code = field_text(root, ("品番",)) or requested_code
        code, family = extract_code(raw_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        runtime = integer_minutes(field_text(root, ("収録時間",)))
        plot_node = root.css_first("#introduction dd")
        plot = plot_node.text(separator="\n", strip=True) if plot_node is not None else None
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=code,
                source_url=url,
                code=code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                release_date=parse_date(field_text(root, ("配信開始日",)) or ""),
                runtime_seconds=runtime * 60 if runtime is not None else None,
                studio=next(iter(field_links(root, ("メーカー",))), None),
                series=next(iter(field_links(root, ("シリーズ",))), None),
                plot=plot,
                actors=field_links(root, ("出演",)),
                tags=field_links(root, ("ジャンル",)),
                artwork=first_image_artwork(root, self._base_url, ("#EnlargeImage",)),
                language="ja",
            )
        ]
