import re
from datetime import date
from urllib.parse import urljoin, urlparse

import httpx
from selectolax.parser import HTMLParser, Node

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError

_FIELD_PATTERNS = {
    "code": re.compile(r"<b>品番</b>:\s*([^<]+)", re.I),
    "release": re.compile(r"<b>配信開始日</b>:\s*(\d{4}-\d{2}-\d{2})", re.I),
    "runtime": re.compile(r"<b>収録時間</b>:\s*(\d+)\s*[^<]*", re.I),
    "actors": re.compile(r"<b>出演者</b>:\s*([^<]+)", re.I),
}
_SCRIPT_TEXT_MARKERS = (
    "adsbyjuicy",
    "videojs(",
    "function ()",
    "window.",
    ".show()",
    ".hide()",
)


class Jav321Provider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="jav321",
            name="Jav321",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        html = await self._post_text(
            self.descriptor.id,
            f"{self._base_url}/search",
            data={"sn": hints.code or hints.term},
        )
        if "AVが見つかりませんでした" in html:
            return []
        return [self._parse(html, hints.code or hints.term)]

    def _parse(self, html: str, requested_code: str) -> ProviderRecord:
        root = HTMLParser(html)
        heading = root.css_first(".panel-heading h3")
        if heading is None:
            raise ProviderError(self.descriptor.id, "parse", "detail heading missing")
        raw_code = _field(html, "code") or requested_code
        code, family = extract_code(raw_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        title = _heading_title(heading)
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")

        source_url = _source_url(root, self._base_url, code)
        release = _date(_field(html, "release"))
        runtime = _integer(_field(html, "runtime"))
        studio = _first_text(root, 'a[href*="/company/"]')
        series = _first_text(root, 'a[href*="/series/"]')
        actors = _actors(_field(html, "actors"), root)
        tags = _link_texts(root, 'a[href*="/genre/"]')
        plot = _plot(root)
        artwork = _artwork(root, self._base_url)
        external_id = urlparse(source_url).path.rstrip("/").split("/")[-1]
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=external_id,
            source_url=source_url,
            code=code,
            title=title,
            original_title=title,
            family=ContentFamily.JAV,
            release_date=release,
            runtime_seconds=runtime * 60 if runtime is not None else None,
            studio=studio,
            series=series,
            plot=plot,
            actors=actors,
            tags=tags,
            artwork=artwork,
            language="ja",
        )


def _field(html: str, name: str) -> str | None:
    match = _FIELD_PATTERNS[name].search(html)
    return match.group(1).replace("&nbsp;", " ").strip() if match else None


def _heading_title(heading: Node) -> str:
    value = heading.text(separator=" ", strip=True)
    small = heading.css_first("small")
    small_text = small.text(separator=" ", strip=True) if small is not None else ""
    if small_text and value.endswith(small_text):
        value = value[: -len(small_text)].strip()
    return value


def _source_url(root: HTMLParser, base_url: str, code: str) -> str:
    for node in root.css("a"):
        if node.text(strip=True) != "简体中文":
            continue
        href = node.attributes.get("href")
        if href:
            return urljoin(base_url + "/", href)
    return f"{base_url}/search?sn={code}"


def _first_text(root: HTMLParser, selector: str) -> str | None:
    node = root.css_first(selector)
    value = node.text(separator=" ", strip=True) if node is not None else ""
    return value or None


def _link_texts(root: HTMLParser, selector: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(value for node in root.css(selector) if (value := node.text(strip=True))))


def _actors(raw: str | None, root: HTMLParser) -> tuple[str, ...]:
    linked = _link_texts(root, 'a[href*="/star/"], a[href*="/heyzo_star/"]')
    if linked:
        return linked
    if not raw:
        return ()
    return tuple(dict.fromkeys(value.strip() for value in re.split(r"[,、/]", raw) if value.strip()))


def _plot(root: HTMLParser) -> str | None:
    candidates: list[str] = []
    for node in root.css(".panel-body > .row .col-md-12"):
        value = node.text(separator=" ", strip=True)
        lowered = value.casefold()
        if (
            len(value) >= 30
            and "人気リスト" not in value
            and not any(marker in lowered for marker in _SCRIPT_TEXT_MARKERS)
        ):
            candidates.append(value.split("※", 1)[0].strip())
    return candidates[0] if candidates else None


def _artwork(root: HTMLParser, base_url: str) -> tuple[Artwork, ...]:
    urls: list[str] = []
    for node in root.css("img.img-responsive[src]"):
        raw = node.attributes.get("src")
        if not raw:
            continue
        url = _image_url(raw, base_url)
        if url not in urls:
            urls.append(url)
    if not urls:
        return ()
    fanart = next((url for url in urls if url.casefold().endswith("pl.jpg")), urls[0])
    poster = next((url for url in urls if url.casefold().endswith("ps.jpg")), urls[0])
    return (
        Artwork.model_validate({"url": fanart, "kind": "fanart"}),
        Artwork.model_validate({"url": poster, "kind": "thumb"}),
    )


def _image_url(raw: str, base_url: str) -> str:
    absolute = urljoin(base_url + "/", raw)
    parsed = urlparse(absolute)
    if parsed.hostname == "pics.dmm.co.jp":
        return urljoin(base_url + "/", parsed.path.lstrip("/"))
    return absolute.replace("http://", "https://", 1)


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value) if value else None
    except ValueError:
        return None


def _integer(value: str | None) -> int | None:
    return int(value) if value and value.isdigit() else None
