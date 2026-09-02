import re
from urllib.parse import quote

import httpx
from selectolax.parser import HTMLParser

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, meta_content

_DASH_CLASS = r"[-\u2013\u2014:]"


class PaipanconProvider(HttpProvider):
    """Paipancon FC2 daily catalog for archived covers and titles."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="paipancon",
            name="Paipancon",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        code, family = extract_code(requested)
        if code is None or family is not ContentFamily.JAV or not code.startswith("FC2-"):
            return []
        number = code.removeprefix("FC2-")
        url = f"{self._base_url}/fc2daily/detail/FC2-PPV-{quote(number)}"
        try:
            html = await self._get_text(self.descriptor.id, url)
        except ProviderError as exc:
            if exc.reason == "http" and "status=404" in exc.detail:
                return []
            raise
        root = HTMLParser(html)
        title = _detail_title(root, number)
        if not title:
            page_title = first_text(root, ("title",)) or ""
            if root.css_first("h2") is None and "FC2-PPV-" not in page_title:
                return []
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        artwork = _artwork(root, self._base_url, number)
        return [
            ProviderRecord(
                provider=self.descriptor.id,
                external_id=number,
                source_url=url,
                code=code,
                title=title,
                original_title=title,
                family=ContentFamily.JAV,
                series="FC2",
                artwork=artwork,
                language="ja",
            )
        ]


def _detail_title(root: HTMLParser, number: str) -> str | None:
    heading = first_text(root, ("h2",))
    if heading:
        cleaned = re.sub(
            rf"(?i)^FC2(?:[-_]?PPV)?[-_]?{re.escape(number)}\s*{_DASH_CLASS}?\s*",
            "",
            heading,
        ).strip()
        if cleaned:
            return cleaned
    page_title = first_text(root, ("title",)) or meta_content(root, "og:title")
    if not page_title:
        return None
    match = re.search(
        rf"(?i)FC2(?:[-_]?PPV)?[-_]?{re.escape(number)}\s*(?:デイリーFC2.*?)?\s*"
        rf"{_DASH_CLASS}\s*(.+?)\s*{_DASH_CLASS}\s*",
        page_title,
    )
    if match:
        candidate = match.group(1).strip()
        if candidate and "デイリー" not in candidate and "パイパン" not in candidate:
            return candidate
    cleaned = re.sub(rf"(?i)FC2(?:[-_]?PPV)?[-_]?{re.escape(number)}", "", page_title)
    cleaned = re.sub(r"(?i)パイパンコン.*$", "", cleaned)
    cleaned = re.sub(
        r"(?i)デイリーFC2|画像プレビュー|動画プレビュー|GIFプレビュー|マグネットLink",
        "",
        cleaned,
    )
    cleaned = re.sub(rf"\s*(?:{_DASH_CLASS[1:-1]}|\|)+\s*", " ", cleaned)
    return cleaned.strip(" -|") or None


def _artwork(root: HTMLParser, base_url: str, number: str) -> tuple[Artwork, ...]:
    values: list[Artwork] = []
    seen: set[str] = set()
    cover = f"{base_url}/fc2daily/data/FC2-PPV-{number}/cover.jpg"
    values.append(Artwork.model_validate({"url": cover, "kind": "fanart"}))
    seen.add(cover)
    selector = f'img[src*="FC2-PPV-{number}"], img[data-src*="FC2-PPV-{number}"]'
    for img in root.css(selector):
        raw = img.attributes.get("src") or img.attributes.get("data-src")
        if not raw:
            continue
        url = raw if raw.startswith("http") else f"{base_url}/{raw.lstrip('/')}"
        if url in seen:
            continue
        kind = "fanart" if "cover" in url else "thumb"
        values.append(Artwork.model_validate({"url": url, "kind": kind}))
        seen.add(url)
        if len(values) >= 4:
            break
    return tuple(values)
