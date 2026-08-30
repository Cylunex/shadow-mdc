import re
from collections.abc import Iterable
from urllib.parse import urljoin

from selectolax.parser import HTMLParser, Node

from ..domain import Artwork


def field_node(root: HTMLParser, labels: Iterable[str]) -> Node | None:
    expected = tuple(_normalize_label(label) for label in labels)
    for node in root.css("th, td, dt, strong"):
        text = _normalize_label(node.text(separator=" ", strip=True))
        if not any(text == label or text.startswith(label) for label in expected):
            continue
        if node.tag == "strong" and node.parent is not None:
            return node.parent
        sibling = node.next
        while sibling is not None and sibling.tag == "-text":
            sibling = sibling.next
        if sibling is not None:
            return sibling
    return None


def field_text(root: HTMLParser, labels: Iterable[str]) -> str | None:
    node = field_node(root, labels)
    value = node.text(separator=" ", strip=True) if node is not None else ""
    return value or None


def field_links(root: HTMLParser, labels: Iterable[str]) -> tuple[str, ...]:
    node = field_node(root, labels)
    if node is None:
        return ()
    return tuple(
        dict.fromkeys(value for link in node.css("a") if (value := link.text(separator=" ", strip=True)))
    )


def first_image_artwork(root: HTMLParser, base_url: str, selectors: Iterable[str]) -> tuple[Artwork, ...]:
    for selector in selectors:
        node = root.css_first(selector)
        if node is None:
            continue
        raw = node.attributes.get("href") or node.attributes.get("data-src") or node.attributes.get("src")
        if raw:
            return (Artwork.model_validate({"url": urljoin(base_url + "/", raw), "kind": "fanart"}),)
    return ()


def integer_minutes(value: str | None) -> int | None:
    match = re.search(r"\d+", value or "")
    return int(match.group()) if match else None


def _normalize_label(value: str) -> str:
    return re.sub(r"\s+", "", value).rstrip(":\uff1a").casefold()
