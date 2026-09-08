"""Magnet URI helpers — list/display/persist only; no download client."""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

_BTIH = re.compile(r"(?i)urn:btih:([a-fA-F0-9]{40}|[a-zA-Z2-7]{32})")
_MAGNET_HREF = re.compile(r"(?i)magnet:\?[^\s\"'<>]+")


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
        size_bytes = None
        if size_values:
            try:
                size_bytes = int(size_values[0])
            except ValueError:
                size_bytes = None
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
    return tuple(found)
