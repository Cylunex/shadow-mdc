"""CosersLove (coserslove.com) homepage cosplay discovery — metadata only.

Album/coser deep pages are often behind Cloudflare; the homepage SSR HTML still
exposes recent album cards (coser name + set title) and featured coser links.
We harvest those for non-JAV actor discovery (group: cosplay). No image bulk
download and no album-page scrape.
"""

from __future__ import annotations

import re
from urllib.parse import urljoin

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_BASE_URL = "https://coserslove.com"
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# [Sayo Momo] 绝区零 - 蕾米埃尔·丹[22P - 992MB]
_ALBUM_ALT_RE = re.compile(
    r"^\[([^\]]+)\]\s*(.+?)(?:\[(\d+)\s*P\s*[-]\s*([^\]]+)\])?\s*$"
)

class CosersloveAlbumCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    album_id: str
    href: str
    coser_name: str
    title: str
    photo_count: int | None = Field(default=None, ge=0)
    size_label: str | None = None
    cover_url: str | None = None


class CosersloveCoserCard(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    coser_id: str
    href: str
    name: str
    avatar_url: str | None = None


class CosersloveHomeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    base_url: str = DEFAULT_BASE_URL
    albums: tuple[CosersloveAlbumCard, ...] = ()
    cosers: tuple[CosersloveCoserCard, ...] = ()
    coser_names: tuple[str, ...] = ()


def parse_home_html(html: str, *, base_url: str = DEFAULT_BASE_URL) -> CosersloveHomeSnapshot:
    if not html or "Just a moment" in html[:2000]:
        return CosersloveHomeSnapshot(base_url=base_url)

    albums: list[CosersloveAlbumCard] = []
    seen_albums: set[str] = set()
    # Album cards: <a href="/album/uuid">…<img alt="[Coser] title[Np - size]">
    for match in re.finditer(
        r'href="(/album/([a-f0-9-]+))"[\s\S]{0,600}?alt="([^"]+)"',
        html,
        re.IGNORECASE,
    ):
        href, album_id, alt = match.group(1), match.group(2), match.group(3)
        if album_id in seen_albums:
            continue
        parsed = _parse_album_alt(alt)
        if parsed is None:
            continue
        seen_albums.add(album_id)
        cover = None
        cover_match = re.search(
            rf'https://img\.coserslove\.com/pic/{re.escape(album_id)}/[^"\s]+',
            match.group(0),
        )
        if cover_match:
            cover = cover_match.group(0)
        albums.append(
            CosersloveAlbumCard(
                album_id=album_id,
                href=urljoin(base_url, href),
                coser_name=parsed[0],
                title=parsed[1],
                photo_count=parsed[2],
                size_label=parsed[3],
                cover_url=cover,
            )
        )

    cosers: list[CosersloveCoserCard] = []
    seen_cosers: set[str] = set()
    for match in re.finditer(
        r'href="(/coser/([a-f0-9-]+))"[\s\S]{0,400}?alt="([^"]+)"',
        html,
        re.IGNORECASE,
    ):
        href, coser_id, name = match.group(1), match.group(2), match.group(3).strip()
        if not name or coser_id in seen_cosers:
            continue
        seen_cosers.add(coser_id)
        avatar = None
        av = re.search(
            rf'https://img\.coserslove\.com/avatars/{re.escape(coser_id)}\.[a-z0-9]+',
            match.group(0),
            re.IGNORECASE,
        )
        if av:
            avatar = av.group(0)
        cosers.append(
            CosersloveCoserCard(
                coser_id=coser_id,
                href=urljoin(base_url, href),
                name=name,
                avatar_url=avatar,
            )
        )

    # Also collect coser names from album cards (often denser than featured strip).
    names: list[str] = []
    seen_names: set[str] = set()
    for coser in cosers:
        key = coser.name.casefold()
        if key not in seen_names:
            seen_names.add(key)
            names.append(coser.name)
    for album in albums:
        key = album.coser_name.casefold()
        if key not in seen_names:
            seen_names.add(key)
            names.append(album.coser_name)

    return CosersloveHomeSnapshot(
        base_url=base_url,
        albums=tuple(albums),
        cosers=tuple(cosers),
        coser_names=tuple(names),
    )


def _parse_album_alt(alt: str) -> tuple[str, str, int | None, str | None] | None:
    cleaned = alt.strip()
    match = _ALBUM_ALT_RE.match(cleaned)
    if match is None:
        return None
    coser = match.group(1).strip()
    title = match.group(2).strip()
    photo_count = int(match.group(3)) if match.group(3) else None
    size_label = match.group(4).strip() if match.group(4) else None
    if not coser or not title:
        return None
    return coser, title, photo_count, size_label
