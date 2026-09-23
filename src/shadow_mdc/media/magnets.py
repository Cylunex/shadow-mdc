"""Magnet URI helpers — list/display/persist only; no download client.

Size pairing ideas adapted from raawaa/jav-scrapy ``extractMagnetLinks``:
pair magnet hrefs with nearby ``N.NNGB|MB`` labels, harden when counts differ,
and prefer largest / subtitle / HD when picking a default.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

_BTIH = re.compile(r"(?i)urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")
_MAGNET_HREF = re.compile(r"(?i)magnet:\?[^\s\"'<>]+")
_SIZE_LABEL = re.compile(r"(?i)(\d+(?:\.\d+)?)\s*(GiB|GB|MiB|MB|KiB|KB|TiB|TB)\b")

class MagnetLink(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    info_hash: str = Field(min_length=32, max_length=64)
    uri: str = Field(min_length=20)
    name: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    has_subtitle: bool = False
    hd: bool = False
    files_count: int | None = Field(default=None, ge=0)


def info_hash_from_uri(uri: str) -> str | None:
    match = _BTIH.search(uri)
    if match is None:
        return None
    value = match.group(1)
    if len(value) == 40:
        return value.upper()
    return value.upper()


def normalize_magnet_uri(uri: str, *, name: str | None = None) -> str:
    cleaned = uri.strip()
    if not cleaned.lower().startswith("magnet:?"):
        raise ValueError("not a magnet URI")
    digest = info_hash_from_uri(cleaned)
    if digest is None:
        raise ValueError("magnet URI missing btih")
    if "xt=urn:btih:" not in cleaned.casefold():
        raise ValueError("magnet URI missing xt")
    if name and "dn=" not in cleaned.casefold():
        from urllib.parse import quote

        cleaned = f"{cleaned}&dn={quote(name)}"
    return cleaned


def parse_size_label_to_bytes(label: str) -> int | None:
    """Parse labels like ``1.45GB`` / ``850 MB`` into bytes."""

    match = _SIZE_LABEL.fullmatch(label.strip())
    if match is None:
        match = _SIZE_LABEL.search(label)
        if match is None:
            return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    factor = {
        "KB": 1000,
        "KIB": 1024,
        "MB": 1000**2,
        "MIB": 1024**2,
        "GB": 1000**3,
        "GIB": 1024**3,
        "TB": 1000**4,
        "TIB": 1024**4,
    }.get(unit)
    if factor is None:
        return None
    return int(value * factor)


def _sizes_from_html(html: str) -> list[int]:
    sizes: list[int] = []
    for match in _SIZE_LABEL.finditer(html):
        parsed = parse_size_label_to_bytes(match.group(0))
        if parsed is not None:
            sizes.append(parsed)
    return sizes


def _size_near_uri(html: str, uri: str) -> int | None:
    """Find a size label in the short window immediately after this magnet href."""

    idx = html.find(uri)
    if idx < 0:
        digest = info_hash_from_uri(uri)
        if digest:
            idx = html.casefold().find(digest.casefold())
        if idx < 0:
            return None
    magnet_end = idx + len(uri)
    # Stop at the next magnet so we do not steal the following row's size.
    next_magnet = _MAGNET_HREF.search(html, magnet_end)
    ahead_end = next_magnet.start() if next_magnet is not None else magnet_end + 200
    ahead = html[magnet_end:ahead_end]
    match = _SIZE_LABEL.search(ahead)
    if match is None:
        return None
    return parse_size_label_to_bytes(match.group(0))


def parse_magnet_links_from_html(html: str, *, provider: str) -> tuple[MagnetLink, ...]:
    found: list[MagnetLink] = []
    seen: set[str] = set()
    for match in _MAGNET_HREF.finditer(html):
        raw = match.group(0).rstrip(").,;]")
        try:
            uri = normalize_magnet_uri(unquote(raw))
        except ValueError:
            continue
        digest = info_hash_from_uri(uri)
        if digest is None or digest in seen:
            continue
        seen.add(digest)
        parsed = urlparse(uri)
        query = parse_qs(parsed.query)
        name_values = query.get("dn") or []
        name = unquote(name_values[0]) if name_values else None
        size_values = query.get("xl") or []
        size_bytes: int | None = None
        if size_values:
            try:
                size_bytes = int(size_values[0])
            except ValueError:
                size_bytes = None
        if size_bytes is None:
            size_bytes = _size_near_uri(html, raw)
        lower_name = (name or "").casefold()
        found.append(
            MagnetLink(
                provider=provider,
                info_hash=digest,
                uri=uri,
                name=name,
                size_bytes=size_bytes,
                has_subtitle="字幕" in (name or "") or "subtitle" in lower_name or "-c" in lower_name,
                hd="1080" in lower_name or "2160" in lower_name or "4k" in lower_name or "hd" in lower_name,
            )
        )
    return tuple(_apply_positional_size_pairing(html, found))


def _apply_positional_size_pairing(html: str, magnets: list[MagnetLink]) -> list[MagnetLink]:
    """jav-scrapy-style index pairing, hardened for unequal magnet/size counts.

    Only fills magnets still missing ``size_bytes``. Uses ``sizes[i]`` for magnet ``i``
    when available — never raises if magnets outnumber sizes (jav-scrapy TODO).
    Requires page-level size-label count within 1 of magnet count to avoid grabbing
    unrelated ``GB`` labels from the rest of the page.
    """

    if not magnets:
        return magnets
    if all(item.size_bytes is not None for item in magnets):
        return magnets
    sizes = _sizes_from_html(html)
    if not sizes:
        return magnets
    if abs(len(sizes) - len(magnets)) > 1:
        return magnets
    filled: list[MagnetLink] = []
    for index, item in enumerate(magnets):
        if item.size_bytes is not None or index >= len(sizes):
            filled.append(item)
            continue
        filled.append(item.model_copy(update={"size_bytes": sizes[index]}))
    return filled


def rank_magnets(magnets: list[MagnetLink] | tuple[MagnetLink, ...]) -> list[MagnetLink]:
    """Prefer subtitle, then HD, then larger size_bytes."""

    return sorted(
        magnets,
        key=lambda item: (
            1 if item.has_subtitle else 0,
            1 if item.hd else 0,
            item.size_bytes or 0,
        ),
        reverse=True,
    )


def pick_best_magnet_link(magnets: list[MagnetLink] | tuple[MagnetLink, ...]) -> MagnetLink | None:
    ranked = rank_magnets(magnets)
    return ranked[0] if ranked else None


def format_size_bytes(size_bytes: int | None) -> str | None:
    if size_bytes is None or size_bytes < 0:
        return None
    if size_bytes >= 1024**3:
        return f"{size_bytes / (1024**3):.2f} GiB"
    if size_bytes >= 1024**2:
        return f"{size_bytes / (1024**2):.0f} MiB"
    if size_bytes >= 1024:
        return f"{size_bytes / 1024:.0f} KiB"
    return f"{size_bytes} B"
