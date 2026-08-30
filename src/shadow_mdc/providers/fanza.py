import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, parse_date
from .html_fields import field_links, field_text, first_image_artwork, integer_minutes


class FanzaProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fanza",
            name="FANZA / DMM",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV or requested_code.startswith("FC2-"):
            return []
        for content_id in _content_id_candidates(requested_code):
            url = f"{self._base_url}/digital/videoa/-/detail/=/cid={quote(content_id)}/"
            try:
                html = await self._get_text(
                    self.descriptor.id,
                    url,
                    headers={
                        "Accept-Language": "ja,en-US;q=0.9",
                        "Cookie": "age_check_done=1",
                    },
                )
            except ProviderError as exc:
                if exc.reason == "http" and "status=404" in exc.detail:
                    continue
                raise
            lowered = html.casefold()
            if "not available in your region" in lowered or "/login/" in lowered:
                raise ProviderError(self.descriptor.id, "blocked", "region or login restriction")
            root = HTMLParser(html)
            if root.css_first(".hreview") is None:
                continue
            return [self._parse(root, url, requested_code, content_id)]
        return []

    def _parse(
        self,
        root: HTMLParser,
        url: str,
        requested: str,
        content_id: str,
    ) -> ProviderRecord:
        title = first_text(root, (".hreview h1", "h1#title", "h1"))
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        raw_code = field_text(root, ("品番",)) or requested
        code, family = extract_code(raw_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        runtime = integer_minutes(field_text(root, ("収録時間",)))
        plot = first_text(root, (".mg-b20.lh4", ".mg-b20.lh4 p", ".product-description"))
        actors = tuple(dict.fromkeys((*field_links(root, ("出演者",)), *tuple(_texts(root, "#performer a")))))
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=content_id,
            source_url=url,
            code=code,
            title=title,
            original_title=title,
            family=ContentFamily.JAV,
            release_date=parse_date(field_text(root, ("配信開始日", "発売日")) or ""),
            runtime_seconds=runtime * 60 if runtime is not None else None,
            studio=next(iter(field_links(root, ("メーカー",))), None),
            label=next(iter(field_links(root, ("レーベル",))), None),
            series=next(iter(field_links(root, ("シリーズ",))), None),
            plot=plot,
            actors=actors,
            directors=field_links(root, ("監督",)),
            tags=field_links(root, ("ジャンル",)),
            artwork=first_image_artwork(
                root,
                self._base_url,
                ("#sample-video a", "img[name=package-image]"),
            ),
            language="ja",
        )


def _content_id_candidates(code: str) -> tuple[str, ...]:
    match = re.fullmatch(r"(?i)([A-Z0-9]+)-(\d+)", code.strip())
    if match is None:
        return (code.replace("-", "").casefold(),)
    prefix, digits = match.groups()
    values = (
        f"{prefix.casefold()}{digits.zfill(5)}",
        f"{prefix.casefold()}{digits}",
        code.replace("-", "").casefold(),
    )
    return tuple(dict.fromkeys(values))


def _texts(root: HTMLParser, selector: str) -> list[str]:
    return [value for node in root.css(selector) if (value := node.text(separator=" ", strip=True))]
