import asyncio
import re
from urllib.parse import quote, urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from ..media.magnets import MagnetLink, parse_magnet_links_from_html
from .html import (
    absolute,
    first_text,
    image_artwork,
    meta_content,
    parse_date,
    parse_runtime_seconds,
)


class JavDBProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="javdb",
            name="JavDB",
            query_modes=frozenset({QueryMode.CODE, QueryMode.TEXT, QueryMode.URL}),
            families=frozenset({ContentFamily.JAV, ContentFamily.CHINESE, ContentFamily.ANIMATION}),
        )

    async def fetch_html(self, url: str, *, params: dict[str, str] | None = None) -> str:
        return await self._get_text(
            self.descriptor.id,
            url,
            params=params,
            headers={
                "User-Agent": (
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/128.0.0.0 Safari/537.36"
                ),
                "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                "Accept-Language": "zh-CN,zh;q=0.9,en-US;q=0.8,en;q=0.7,ja;q=0.6",
            },
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        if hints.mode is QueryMode.URL and hints.source_url:
            return [await self._detail(hints.source_url)]
        url = f"{self._base_url}/search?q={quote(hints.term)}&f=all"
        html = await self._get_text(self.descriptor.id, url, params={"locale": "zh"})
        root = HTMLParser(html)
        links: list[str] = []
        for selector in (".movie-list .item a", "a.box", ".grid-item a"):
            for node in root.css(selector):
                href = node.attributes.get("href")
                if href and "/v/" in href:
                    absolute = urljoin(self._base_url + "/", href)
                    if absolute not in links:
                        links.append(absolute)
        canonical = root.css_first('link[rel="canonical"]')
        if not links and canonical is not None:
            href = canonical.attributes.get("href")
            if href and "/v/" in href:
                links.append(urljoin(self._base_url + "/", href))
        results = await asyncio.gather(*(self._detail(link) for link in links[:5]), return_exceptions=True)
        records = [result for result in results if isinstance(result, ProviderRecord)]
        if links and not records:
            failure = next((result for result in results if isinstance(result, Exception)), None)
            if failure is not None:
                raise failure
        return records

    async def _detail(self, url: str) -> ProviderRecord:
        html = await self._get_text(self.descriptor.id, url, params={"locale": "zh"})
        return parse_javdb_detail(html, url)

    async def magnets(self, external_id_or_url: str) -> tuple[MagnetLink, ...]:
        """List magnet URIs from the public detail page (display/save only)."""

        if external_id_or_url.startswith("http://") or external_id_or_url.startswith("https://"):
            url = external_id_or_url
        else:
            url = f"{self._base_url}/v/{external_id_or_url}"
        html = await self._get_text(self.descriptor.id, url, params={"locale": "zh"})
        return parse_magnet_links_from_html(html, provider=self.descriptor.id)


# JavDB's site chrome (navbar dropdowns, footer) links to ``/actors/censored``,
# ``/tags/uncensored``, ``/makers/uncensored`` … with labels like 有碼/無碼/歐美/FC2/動漫.
# A page-wide ``a[href*="/actors/"]`` scrape therefore turns nav labels into
# "actors"; all metadata must come from the detail panel (``.movie-panel-info``).
_SITE_DESCRIPTION_PREFIXES = ("番號搜磁鏈", "番号搜磁链")
_CHROME_ANCESTOR_TAGS = frozenset({"header", "footer"})
_CHROME_ANCESTOR_CLASSES = frozenset(
    {"navbar", "navbar-menu", "navbar-dropdown", "main-tabs", "tabs", "footer", "sub-header"}
)
_PANEL_LABELS: dict[str, tuple[str, ...]] = {
    "code": ("番號", "番号", "ID"),
    "date": ("日期", "發行日期", "发行日期", "Released Date", "Date"),
    "runtime": ("時長", "时长", "Duration"),
    "directors": ("導演", "导演", "Director"),
    "studio": ("片商", "Maker"),
    "label": ("發行", "发行", "Publisher"),
    "series": ("系列", "Series"),
    "rating": ("評分", "评分", "Rating"),
    "tags": ("類別", "类别", "Tags"),
    "actors": ("演員", "演员", "Actor(s)", "Actors", "Actor"),
}


def _panel_blocks(root: HTMLParser) -> dict[str, Node]:
    blocks: dict[str, Node] = {}
    for block in root.css(".movie-panel-info .panel-block"):
        label_node = block.css_first("strong")
        if label_node is None:
            continue
        label = label_node.text(strip=True).rstrip(":：").strip()
        for key, names in _PANEL_LABELS.items():
            if label in names and key not in blocks:
                blocks[key] = block
    return blocks


def _value_node(block: Node) -> Node:
    return block.css_first(".value") or block


def _value_text(block: Node | None) -> str | None:
    if block is None:
        return None
    text = _value_node(block).text(separator=" ", strip=True)
    text = re.sub(r"\s+", " ", text).strip()
    return text or None


def _value_links(block: Node | None) -> tuple[str, ...]:
    if block is None:
        return ()
    values: list[str] = []
    for node in _value_node(block).css("a"):
        text = node.text(separator=" ", strip=True)
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _actor_gender(anchor: Node) -> str | None:
    classes = (anchor.attributes.get("class") or "").split()
    if "actor-female" in classes or "female" in classes:
        return "female"
    if "actor-male" in classes or "male" in classes:
        return "male"
    # Older layout: ``<a href="/actors/x">Name</a><strong class="symbol female">♀</strong>``.
    sibling = anchor.next
    while sibling is not None and sibling.tag in {"-text"} and not sibling.text(strip=True).strip(",、 "):
        sibling = sibling.next
    if sibling is not None and sibling.tag == "strong":
        symbol = (sibling.attributes.get("class") or "").split()
        if "female" in symbol:
            return "female"
        if "male" in symbol:
            return "male"
    return None


def _panel_actors(block: Node | None) -> tuple[str, ...]:
    """Female performers only when JavDB marks gender; all names when it does not."""

    if block is None:
        return ()
    entries: list[tuple[str, str | None]] = []
    for anchor in _value_node(block).css('a[href*="/actors/"]'):
        name = anchor.text(separator=" ", strip=True)
        if name and name not in {item[0] for item in entries}:
            entries.append((name, _actor_gender(anchor)))
    if any(gender is not None for _name, gender in entries):
        return tuple(name for name, gender in entries if gender == "female")
    return tuple(name for name, _gender in entries)


def _in_site_chrome(node: Node) -> bool:
    current = node.parent
    while current is not None:
        if current.tag in _CHROME_ANCESTOR_TAGS:
            return True
        classes = set((current.attributes.get("class") or "").split())
        if classes & _CHROME_ANCESTOR_CLASSES:
            return True
        current = current.parent
    return False


def _content_link_texts(root: HTMLParser, selector: str) -> tuple[str, ...]:
    values: list[str] = []
    for node in root.css(selector):
        if _in_site_chrome(node):
            continue
        text = node.text(separator=" ", strip=True)
        if text and text not in values:
            values.append(text)
    return tuple(values)


def _parse_rating(text: str | None) -> tuple[float | None, int | None]:
    if not text:
        return None, None
    value_match = re.search(r"(\d+(?:\.\d+)?)\s*分", text)
    count_match = re.search(r"由\s*(\d+)\s*人", text) or re.search(r"(\d+)\s*(?:users|人)", text)
    value = float(value_match.group(1)) if value_match else None
    if value is not None and value > 5:
        value = None
    count = int(count_match.group(1)) if count_match else None
    return value, count


def parse_javdb_detail(html: str, url: str) -> ProviderRecord:
    """Parse a JavDB ``/v/<id>`` detail page into a provider record."""

    root = HTMLParser(html)
    title_node = root.css_first("h2.title")
    title: str | None = None
    code_text: str | None = None
    if title_node is not None:
        current = title_node.css_first("strong.current-title")
        if current is not None:
            title = current.text(separator=" ", strip=True) or None
            first = title_node.css_first("strong")
            if first is not None and first is not current:
                code_text = first.text(strip=True) or None
        else:
            title = title_node.text(separator=" ", strip=True) or None
    if title is None:
        title = first_text(root, ("h1.title",)) or meta_content(root, "og:title")
    if title is None:
        raise ProviderError("javdb", "parse", "detail title missing")
    # zh locale shows a Chinese machine/community translation in ``current-title`` for some
    # works and keeps the Japanese original in a hidden ``.origin-title``; like the other JAV
    # providers we store the original (the app translates on its own).
    origin = first_text(root, ("h2.title .origin-title", ".origin-title"))
    if origin:
        title = origin

    blocks = _panel_blocks(root)
    clipboard = root.css_first(".movie-panel-info .first-block [data-clipboard-text]")
    panel_code = (
        clipboard.attributes.get("data-clipboard-text") if clipboard is not None else None
    ) or _value_text(blocks.get("code"))
    parsed_code, family = None, ContentFamily.UNKNOWN
    for candidate in (panel_code, code_text, title):
        if candidate:
            parsed_code, family = extract_code(candidate)
            if parsed_code is not None:
                break

    external_id = urlparse(url).path.rstrip("/").split("/")[-1]
    if blocks:
        release_date = parse_date(_value_text(blocks.get("date")) or "")
        runtime = parse_runtime_seconds(_value_text(blocks.get("runtime")) or "")
        studio_links = _value_links(blocks.get("studio"))
        label_links = _value_links(blocks.get("label"))
        series_links = _value_links(blocks.get("series"))
        studio = studio_links[0] if studio_links else _value_text(blocks.get("studio"))
        label = label_links[0] if label_links else None
        series = series_links[0] if series_links else None
        directors = _value_links(blocks.get("directors"))
        actors = _panel_actors(blocks.get("actors"))
        tags = _value_links(blocks.get("tags"))
        rating, rating_count = _parse_rating(_value_text(blocks.get("rating")))
    else:
        # Legacy/minimal markup without the info panel: scrape content links but
        # never site chrome (navbar dropdowns carry 有碼/無碼/歐美 category links).
        full_text = root.text(separator=" ", strip=True)
        release_date = parse_date(full_text)
        runtime = parse_runtime_seconds(full_text)
        studios = _content_link_texts(root, 'a[href*="/makers/"]') or _content_link_texts(
            root, 'a[href*="/publishers/"]'
        )
        studio = studios[0] if studios else None
        label = None
        series = None
        directors = ()
        actors = _content_link_texts(root, 'a[href*="/actors/"]') or _content_link_texts(
            root, 'a[href*="/performers/"]'
        )
        tags = _content_link_texts(root, 'a[href*="/tags"]')
        rating, rating_count = None, None

    plot = meta_content(root, "description")
    if plot and plot.startswith(_SITE_DESCRIPTION_PREFIXES):
        plot = None
    artwork = image_artwork(root, url)
    if not artwork:
        cover = root.css_first("img.video-cover")
        cover_url = absolute(url, cover.attributes.get("src") or cover.attributes.get("data-src")) if cover else None
        if cover_url and cover_url.startswith(("http://", "https://")):
            artwork = (Artwork.model_validate({"url": cover_url, "kind": "thumb"}),)

    return ProviderRecord(
        provider="javdb",
        external_id=external_id,
        source_url=url,
        code=parsed_code,
        title=title,
        original_title=title,
        family=family,
        release_date=release_date,
        runtime_seconds=runtime,
        studio=studio,
        label=label,
        series=series,
        plot=plot,
        actors=actors,
        directors=directors,
        tags=tags,
        artwork=artwork,
        language="zh",
        rating=rating,
        rating_max=5.0 if rating is not None else None,
        rating_count=rating_count,
    )
