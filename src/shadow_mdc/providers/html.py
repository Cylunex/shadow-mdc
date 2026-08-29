import re
from datetime import date
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from ..domain import Artwork


def first_text(root: HTMLParser | Node, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        node = root.css_first(selector)
        if node is not None:
            text = node.text(separator=" ", strip=True)
            if text:
                return text
    return None


def meta_content(root: HTMLParser, property_name: str) -> str | None:
    node = root.css_first(f'meta[property="{property_name}"]') or root.css_first(
        f'meta[name="{property_name}"]'
    )
    value = node.attributes.get("content") if node is not None else None
    return value.strip() if value else None


def absolute(base_url: str, value: str | None) -> str | None:
    return urljoin(base_url.rstrip("/") + "/", value) if value else None


def link_texts(root: HTMLParser, selectors: tuple[str, ...]) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for selector in selectors:
        for node in root.css(selector):
            text = node.text(separator=" ", strip=True)
            if text and text not in seen:
                values.append(text)
                seen.add(text)
    return tuple(values)


def parse_date(text: str) -> date | None:
    match = re.search(r"\b(20\d{2})[-/.](\d{1,2})[-/.](\d{1,2})\b", text)
    if match is None:
        return None
    try:
        return date(*(int(value) for value in match.groups()))
    except ValueError:
        return None


def parse_runtime_seconds(text: str) -> int | None:
    match = re.search(r"(?i)(\d{1,4})\s*(?:min|mins|minutes|分鐘|分钟|分)", text)
    return int(match.group(1)) * 60 if match else None


def image_artwork(root: HTMLParser, base_url: str) -> tuple[Artwork, ...]:
    candidates = [
        (meta_content(root, "og:image"), "thumb"),
        (meta_content(root, "twitter:image"), "thumb"),
    ]
    values: list[Artwork] = []
    seen: set[str] = set()
    for raw, kind in candidates:
        url = absolute(base_url, raw)
        if url and url not in seen and url.startswith(("http://", "https://")):
            values.append(Artwork.model_validate({"url": url, "kind": kind}))
            seen.add(url)
    return tuple(values)
