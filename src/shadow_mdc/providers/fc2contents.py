import re
from datetime import date
from urllib.parse import quote, urljoin

import httpx
from selectolax.parser import HTMLParser

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, link_texts, meta_content, parse_date


class Fc2ContentsProvider(HttpProvider):
    """Official FC2 Contents Market (adult.contents.fc2.com)."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fc2contents",
            name="FC2 Contents Market",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        code, family = extract_code(requested)
        if code is None or family is not ContentFamily.JAV or not code.startswith("FC2-"):
            return []
        number = code.removeprefix("FC2-")
        url = f"{self._base_url}/article/{quote(number)}/"
        html = await self._get_text(self.descriptor.id, url)
        root = HTMLParser(html)
        if root.css_first(".items_article_headerInfo") is None:
            return []
        title = _detail_title(root, number)
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        full_text = root.text(separator=" ", strip=True)
        studio = first_text(root, (".items_article_writer a",))
        plot = _plot(root)
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=number,
                source_url=url,
                code=code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                release_date=parse_date(full_text) or _sale_date(root),
                runtime_seconds=_runtime_seconds(root),
                studio=studio,
                series="FC2",
                plot=plot,
                tags=link_texts(root, (".items_article_TagArea a.tag", "a.tagTag")),
                artwork=_artwork(root, self._base_url),
                language="ja",
            )
        ]


def _detail_title(root: HTMLParser, number: str) -> str | None:
    og = meta_content(root, "og:title")
    if og:
        cleaned = re.sub(rf"(?i)^FC2(?:[-_]?PPV)?[-_]?{re.escape(number)}\s*", "", og).strip()
        return cleaned or og
    heading = root.css_first('.items_article_headerInfo h3, [data-section="userInfo"] h3')
    if heading is None:
        return None
    html = heading.html or ""
    html = re.sub(
        r'<span[^>]*style="[^"]*(?:zoom:\s*0\.01|overflow:\s*hidden)[^"]*"[^>]*>.*?</span>',
        "",
        html,
        flags=re.IGNORECASE | re.DOTALL,
    )
    text = HTMLParser(html).text(separator=" ", strip=True)
    text = re.sub(r"\*{3}\S*", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _plot(root: HTMLParser) -> str | None:
    for key in ("description", "og:description"):
        value = meta_content(root, key)
        if not value:
            continue
        cleaned = re.sub(r"(?i)^FC2(?:[-_]?PPV)?[-_]?\d+\s*[\u2015\-]?\s*", "", value).strip()
        cleaned = re.sub(r"\s+", " ", cleaned)
        if cleaned:
            return cleaned
    return None


def _sale_date(root: HTMLParser) -> date | None:
    for node in root.css(".items_article_softDevice p, p"):
        text = node.text(separator=" ", strip=True)
        if "販売日" in text:
            return parse_date(text)
    return None


def _runtime_seconds(root: HTMLParser) -> int | None:
    raw = first_text(root, (".items_article_info", ".items_article_MainitemThumb .items_article_info"))
    if not raw:
        return None
    match = re.fullmatch(r"(\d{1,3}):([0-5]\d)", raw.strip())
    if match is None:
        return None
    return int(match.group(1)) * 60 + int(match.group(2))


def _artwork(root: HTMLParser, base_url: str) -> tuple[Artwork, ...]:
    values: list[Artwork] = []
    seen: set[str] = set()

    def add(raw: str | None, kind: str) -> None:
        url = _absolute(base_url, raw)
        if url and url not in seen and url.startswith(("http://", "https://")):
            values.append(Artwork.model_validate({"url": url, "kind": kind}))
            seen.add(url)

    for node in root.css("ul.items_article_SampleImagesArea a[href]"):
        add(node.attributes.get("href"), "fanart")
        if len(values) >= 3:
            break
    add(meta_content(root, "og:image"), "thumb")
    thumb = root.css_first(".items_article_MainitemThumb img")
    if thumb is not None:
        add(thumb.attributes.get("src"), "thumb")
    return tuple(values)


def _absolute(base_url: str, value: str | None) -> str | None:
    if not value:
        return None
    if value.startswith("//"):
        return "https:" + value
    return urljoin(base_url.rstrip("/") + "/", value)
