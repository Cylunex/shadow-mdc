"""Sukebei (nyaa) magnet search by code — list-only fallback when JavDB has none.

Ideas (query variants, per-row seeders/size parsing, screenshot links from the
view page description) borrowed from the user's private BT search skill; code is
written from scratch.
"""

from __future__ import annotations

import html as html_lib
import re
from dataclasses import dataclass, field
from urllib.parse import urljoin

import httpx
from selectolax.parser import HTMLParser

from ..media.magnets import (
    MagnetLink,
    info_hash_from_uri,
    magnet_quality_flags,
    normalize_magnet_uri,
    parse_size_label_to_bytes,
)
from .base import HttpProvider

PROVIDER_ID = "sukebei"
DEFAULT_BASE_URL = "https://sukebei.nyaa.si"
# 2_2 = Real Life - Videos (JAV lives here); 0_0 = all as the last resort.
_CATEGORIES: tuple[str, ...] = ("2_2", "0_0")
_CODE_PARTS = re.compile(r"^([A-Za-z]+\d*?)[-_ ]?(\d+)([A-Za-z]?)$")
_IMAGE_URL = re.compile(r"https?://[^\s<>\"')\]]+?\.(?:jpe?g|png|webp|gif)(?:\?[^\s<>\"')\]]*)?", re.IGNORECASE)
_SKIP_IMAGE = re.compile(r"avatar|logo|advert|/static/|default\.png", re.IGNORECASE)


@dataclass(frozen=True)
class SukebeiRow:
    title: str
    view_url: str
    magnet: str
    size_bytes: int | None
    seeders: int
    leechers: int
    downloads: int
    timestamp: int | None = None
    screenshots: tuple[str, ...] = field(default_factory=tuple)


def query_variants(code: str) -> list[str]:
    """``SSIS-001`` → ``["SSIS-001", "SSIS001"]``; also strips leading zeros (``SSIS-1``)."""

    cleaned = code.strip()
    variants = [cleaned]
    match = _CODE_PARTS.match(cleaned)
    if match:
        prefix, number, suffix = match.groups()
        variants.append(f"{prefix}{number}{suffix}")
        stripped = number.lstrip("0") or "0"
        if stripped != number and len(stripped) >= 2:
            variants.append(f"{prefix}-{stripped}{suffix}")
    seen: set[str] = set()
    out: list[str] = []
    for item in variants:
        key = item.casefold()
        if key not in seen:
            seen.add(key)
            out.append(item)
    return out


def code_pattern(code: str) -> re.Pattern[str]:
    """Regex that matches ``code`` in a release title with flexible separator/zero padding."""

    match = _CODE_PARTS.match(code.strip())
    if not match:
        return re.compile(rf"(?<![A-Za-z0-9]){re.escape(code.strip())}(?![0-9])", re.IGNORECASE)
    prefix, number, suffix = match.groups()
    digits = number.lstrip("0") or "0"
    return re.compile(
        rf"(?<![A-Za-z0-9]){re.escape(prefix)}[-_ ]?0*{digits}{re.escape(suffix)}(?![0-9])",
        re.IGNORECASE,
    )


def _int(text: str) -> int:
    try:
        return int(text.replace(",", "").strip())
    except ValueError:
        return 0


def parse_search_html(page: str, *, base_url: str = DEFAULT_BASE_URL) -> list[SukebeiRow]:
    root = HTMLParser(page)
    rows: list[SukebeiRow] = []
    for tr in root.css("table.torrent-list tbody tr"):
        title_link = None
        for anchor in tr.css('a[href^="/view/"]'):
            href = anchor.attributes.get("href") or ""
            if "#" not in href:
                title_link = anchor
                break
        magnet_link = tr.css_first('a[href^="magnet:"]')
        if title_link is None or magnet_link is None:
            continue
        magnet = html_lib.unescape(magnet_link.attributes.get("href") or "")
        if info_hash_from_uri(magnet) is None:
            continue
        title = (title_link.attributes.get("title") or title_link.text(strip=True)).strip()
        cells = tr.css("td.text-center")
        size_bytes: int | None = None
        timestamp: int | None = None
        numbers: list[int] = []
        for cell in cells:
            text = cell.text(strip=True)
            ts = cell.attributes.get("data-timestamp")
            if ts and ts.isdigit():
                timestamp = int(ts)
            elif size_bytes is None and parse_size_label_to_bytes(text) is not None:
                size_bytes = parse_size_label_to_bytes(text)
            elif text.replace(",", "").isdigit():
                numbers.append(_int(text))
        seeders, leechers, downloads = (numbers + [0, 0, 0])[:3]
        rows.append(
            SukebeiRow(
                title=title,
                view_url=urljoin(base_url.rstrip("/") + "/", title_link.attributes.get("href") or ""),
                magnet=magnet,
                size_bytes=size_bytes,
                seeders=seeders,
                leechers=leechers,
                downloads=downloads,
                timestamp=timestamp,
            )
        )
    return rows


def extract_screenshots(view_html: str, *, limit: int = 5) -> tuple[str, ...]:
    """Direct image URLs from a view page's markdown description (+ any rendered <img>)."""

    root = HTMLParser(view_html)
    sources: list[str] = []
    description = root.css_first("#torrent-description")
    if description is not None:
        sources.extend(_IMAGE_URL.findall(html_lib.unescape(description.text())))
    for img in root.css("#torrent-description img, .panel-body img"):
        src = img.attributes.get("src") or ""
        if src.startswith("http"):
            sources.append(src)
    out: list[str] = []
    for url in sources:
        if _SKIP_IMAGE.search(url) or url in out:
            continue
        out.append(url)
        if len(out) >= limit:
            break
    return tuple(out)


def rows_to_magnets(rows: list[SukebeiRow], code: str) -> tuple[MagnetLink, ...]:
    """Keep rows whose title carries ``code``, dedupe by info-hash, best-seeded first."""

    pattern = code_pattern(code)
    best: dict[str, SukebeiRow] = {}
    for row in rows:
        if not pattern.search(row.title):
            continue
        digest = (info_hash_from_uri(row.magnet) or "").casefold()
        if digest and (digest not in best or row.seeders > best[digest].seeders):
            best[digest] = row
    ordered = sorted(best.values(), key=lambda r: (r.seeders, r.downloads), reverse=True)
    magnets: list[MagnetLink] = []
    for row in ordered:
        subtitle, hd = magnet_quality_flags(row.title)
        try:
            uri = normalize_magnet_uri(row.magnet, name=row.title)
        except ValueError:
            continue
        magnets.append(
            MagnetLink(
                provider=PROVIDER_ID,
                info_hash=info_hash_from_uri(uri) or "",
                uri=uri,
                name=row.title[:300],
                size_bytes=row.size_bytes,
                has_subtitle=subtitle,
                hd=hd,
            )
        )
    return tuple(magnets)


class SukebeiClient(HttpProvider):
    """Code → magnets. Uses HttpProvider for retries, challenge detection and curl_cffi fallback."""

    def __init__(self, client: httpx.AsyncClient, base_url: str = DEFAULT_BASE_URL, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    async def search_rows(self, code: str, *, max_queries: int = 3) -> list[SukebeiRow]:
        pattern = code_pattern(code)
        for category in _CATEGORIES:
            for query in query_variants(code)[:max_queries]:
                page = await self._get_text(
                    PROVIDER_ID,
                    f"{self._base_url}/",
                    params={"f": "0", "c": category, "q": query, "s": "seeders", "o": "desc"},
                )
                rows = [row for row in parse_search_html(page, base_url=self._base_url) if pattern.search(row.title)]
                if rows:
                    return rows
        return []

    async def magnets(self, code: str) -> tuple[MagnetLink, ...]:
        return rows_to_magnets(await self.search_rows(code), code)

    async def screenshots(self, view_url: str, *, limit: int = 5) -> tuple[str, ...]:
        return extract_screenshots(await self._get_text(PROVIDER_ID, view_url), limit=limit)
