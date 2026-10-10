"""Magnet URI helpers — list/display/persist only; no download client.

Size pairing ideas adapted from raawaa/jav-scrapy ``extractMagnetLinks``:
pair magnet hrefs with nearby ``N.NNGB|MB`` labels, harden when counts differ,
and prefer largest / subtitle / HD when picking a default.

Release-name ↔ work-code match (``magnet_code_match``) is inspired by
SilenceSik/media-indexer ``magnet_judge`` (MIT): prefer magnets whose ``dn``
extracts the same 番号 as the Work, demote clear conflicts. Reimplemented with
our ``extract_code`` / ``to_comparison_key`` — no source copy.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qs, unquote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from ..identity import extract_code
from ..normalize_code import to_comparison_key

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
        subtitle, hd = magnet_quality_flags(name)
        found.append(
            MagnetLink(
                provider=provider,
                info_hash=digest,
                uri=uri,
                name=name,
                size_bytes=size_bytes,
                has_subtitle=subtitle,
                hd=hd,
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


# Magnet quality scoring adapted from Teamper/JHS ``src/core/magnet-quality.js`` (MIT):
# subtitle and resolution signals from the release name, with a penalty for
# trailers/samples. Seeders/freshness are not available to us and are omitted.
_SUBTITLE_NAME = re.compile(
    r"(?i)(?:[-_.](?:u?c|ch)(?=$|[\s._\-\[\](){}])|chinese|中字|中文字幕|字幕|subtitle|\bsub\b)"
)
_RES_4K = re.compile(r"(?i)(?:\b4k\b|2160p|\buhd\b)")
_RES_1080 = re.compile(r"(?i)(?:1080[pi]|\bfhd\b|fullhd)")
_RES_720 = re.compile(r"(?i)720p")
_HD_NAME = re.compile(r"(?i)(?:\bhd\b|-hd(?=$|[\s._\-]))")
_SAMPLE_NAME = re.compile(r"(?i)(?:sample|trailer|preview|预告|預告)")
# Below this a "full" release is almost certainly a sample/trailer clip.
_TINY_RELEASE_BYTES = 300 * 1024 * 1024


def magnet_code_match(name: str | None, expected_code: str | None) -> bool | None:
    """Whether a magnet release name belongs to ``expected_code``.

    Returns:
      * ``True`` — name extracts a code that matches ``expected_code``
      * ``False`` — name extracts a *different* code (likely wrong title)
      * ``None`` — no expected code, empty name, or no extractable code
        (keep the magnet; JavDB App API often omits a useful ``dn``)
    """

    if not expected_code or not (name or "").strip():
        return None
    expected_key = to_comparison_key(expected_code)
    if not expected_key:
        return None
    extracted, _family = extract_code(name or "")
    if not extracted:
        return None
    found_key = to_comparison_key(extracted)
    if not found_key:
        return None
    return found_key == expected_key


def _code_match_rank(name: str | None, expected_code: str | None) -> int:
    """Sort tier: matching code (2) > unknown (1) > conflicting code (0)."""

    verdict = magnet_code_match(name, expected_code)
    if verdict is True:
        return 2
    if verdict is False:
        return 0
    return 1


def magnet_quality_flags(name: str | None) -> tuple[bool, bool]:
    """``(has_subtitle, hd)`` inferred from a release name.

    ``-C`` / ``-UC`` / ``-CH`` suffixes mean Chinese subtitles; ``-CD1`` does not.
    """

    text = (name or "").strip()
    if not text:
        return False, False
    subtitle = _SUBTITLE_NAME.search(text) is not None
    hd = bool(_RES_4K.search(text) or _RES_1080.search(text) or _HD_NAME.search(text))
    return subtitle, hd


def magnet_quality_score(
    *,
    name: str | None,
    size_bytes: int | None,
    has_subtitle: bool,
    hd: bool,
) -> int:
    """0–100 quality score: subtitle 20, 4K 25 / 1080p 20 / 720p 15 / unknown 5, sample −40."""

    text = name or ""
    name_subtitle, name_hd = magnet_quality_flags(text)
    score = 20 if (has_subtitle or name_subtitle) else 0
    if _RES_4K.search(text):
        score += 25
    elif _RES_1080.search(text) or hd or name_hd:
        score += 20
    elif _RES_720.search(text):
        score += 15
    else:
        score += 5
    if _SAMPLE_NAME.search(text):
        score -= 40
    if size_bytes is not None and 0 < size_bytes < _TINY_RELEASE_BYTES:
        score -= 15
    return max(0, min(100, score))


def magnet_sort_key(
    *,
    name: str | None,
    size_bytes: int | None,
    has_subtitle: bool,
    hd: bool,
    expected_code: str | None = None,
) -> tuple[int, int, int]:
    """Sort key (descending): code match, quality score, then larger size."""

    return (
        _code_match_rank(name, expected_code),
        magnet_quality_score(name=name, size_bytes=size_bytes, has_subtitle=has_subtitle, hd=hd),
        size_bytes or 0,
    )


def rank_magnets(
    magnets: list[MagnetLink] | tuple[MagnetLink, ...],
    *,
    expected_code: str | None = None,
) -> list[MagnetLink]:
    """Best first: code match, quality score (samples penalised), then size."""

    return sorted(
        magnets,
        key=lambda item: magnet_sort_key(
            name=item.name,
            size_bytes=item.size_bytes,
            has_subtitle=item.has_subtitle,
            hd=item.hd,
            expected_code=expected_code,
        ),
        reverse=True,
    )


def pick_best_magnet_link(
    magnets: list[MagnetLink] | tuple[MagnetLink, ...],
    *,
    expected_code: str | None = None,
) -> MagnetLink | None:
    ranked = rank_magnets(magnets, expected_code=expected_code)
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
