"""ASCII remaster / remake suffix identity (HD / AI / H, optional E/Z).

Ideas from javinizer-go matcher + r18devdump marker-aware candidates (MIT);
implemented in shadow-mdc style — no GPL wholesale copy.

Filenames and display IDs often carry a trailing remaster marker
(``RCT-156HD``, ``RCT-156-H``, ``IPX-535Z-HD``). Metadata lookup uses the
**base** 番号 (marker peeled). Content-id expansion re-attaches a folded
marker (display ``HD`` → ``h``). Leading quality tokens such as ``4K`` are
not the JAV id — ``clean_stem`` already strips those.
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_DISPLAY_SEPARATORS = frozenset("-_. ")
_T28 = "t28"

_DISPLAY_REMASTER = re.compile(
    r"(?i)^(?P<series>t28|[a-z]{1,10})[-_.\s]+(?P<number>\d{1,6})"
    r"(?:[-_.\s]*(?P<ez>[ez]))?[-_.\s]*(?P<marker>hd|ai|h)$"
)
_FUSED_REMASTER = re.compile(
    r"(?i)^(?P<series>t28|[a-z]{1,10})(?P<number>\d{1,6})(?P<ez>[ez]?)"
    r"(?P<marker>hd|ai|h)$"
)
_PREFIXED_CID_REMASTER = re.compile(
    r"(?i)^(?P<prefix>\d+)(?P<series>t28|[a-z]+)(?P<number>\d+)(?P<ez>[ez]?)"
    r"(?P<marker>hd|ai|h)$"
)
_UNDERSCORE_CID = re.compile(r"(?i)^[a-z]_\d+[a-z]+\d+")


@dataclass(frozen=True, slots=True)
class RemasterIdentity:
    """Parsed remaster-bearing id.

    ``base`` — lookup 番号 without remaster marker (E/Z stays on the base).
    ``display`` — canonical catalog spelling with folded marker (HD→H).
    ``content_marker`` — lowercase marker for content_id tails.
    """

    base: str
    display: str
    catalog_suffix: str
    marker: str
    content_marker: str
    had_separator: bool
    raw: str


def parse_remaster_identity(value: str) -> RemasterIdentity | None:
    """Parse a remaster-bearing DVD / content id, or ``None`` if no marker."""

    raw = unicodedata.normalize("NFKC", value or "").strip()
    if not raw:
        return None
    lowered = " ".join(raw.casefold().split())
    # Underscore PPV cids are opaque; do not peel markers for expansion.
    if _UNDERSCORE_CID.match(lowered):
        return None
    has_separator = any(ch in lowered for ch in _DISPLAY_SEPARATORS)
    compacted = lowered.translate(str.maketrans("", "", "-_. "))

    if has_separator:
        match = _DISPLAY_REMASTER.fullmatch(lowered)
        if match is not None:
            return _from_parts(
                series=match.group("series"),
                number=match.group("number"),
                ez=(match.group("ez") or "").casefold(),
                raw_marker=match.group("marker").casefold(),
                has_separator=True,
                fold_hd=True,
                raw=raw,
            )

    match = _PREFIXED_CID_REMASTER.fullmatch(compacted)
    if match is not None:
        number = match.group("number")
        # Drop zero-padding for the human base id (00156 → 156).
        if number.isdigit():
            number = str(int(number))
        fold_hd = has_separator or not (
            _looks_like_content_id(compacted) or _wide_numeric(compacted)
        )
        return _from_parts(
            series=match.group("series"),
            number=number,
            ez=(match.group("ez") or "").casefold(),
            raw_marker=match.group("marker").casefold(),
            has_separator=has_separator,
            fold_hd=fold_hd,
            raw=raw,
        )

    match = _FUSED_REMASTER.fullmatch(compacted)
    if match is not None:
        fold_hd = has_separator or not (
            _looks_like_content_id(compacted) or _wide_numeric(compacted)
        )
        return _from_parts(
            series=match.group("series"),
            number=match.group("number"),
            ez=(match.group("ez") or "").casefold(),
            raw_marker=match.group("marker").casefold(),
            has_separator=has_separator,
            fold_hd=fold_hd,
            raw=raw,
        )
    return None


def strip_remaster_marker(value: str) -> str:
    """Return the base 番号 used for metadata lookup (marker peeled)."""

    parsed = parse_remaster_identity(value)
    if parsed is None:
        return (value or "").strip()
    return parsed.base


def remaster_display(value: str) -> str | None:
    """Canonical remaster display id, or ``None`` when no marker is present."""

    parsed = parse_remaster_identity(value)
    return None if parsed is None else parsed.display


def _from_parts(
    *,
    series: str,
    number: str,
    ez: str,
    raw_marker: str,
    has_separator: bool,
    fold_hd: bool,
    raw: str,
) -> RemasterIdentity:
    if raw_marker == "hd":
        content_marker = "h" if fold_hd else "hd"
        display_marker = "H"
    elif raw_marker == "ai":
        content_marker = "ai"
        display_marker = "AI"
    else:
        content_marker = "h"
        display_marker = "H"
    base = _format_base(series, number, ez)
    return RemasterIdentity(
        base=base,
        display=f"{base}{display_marker}",
        catalog_suffix=ez.upper(),
        marker=display_marker,
        content_marker=content_marker,
        had_separator=has_separator,
        raw=raw,
    )


def _format_base(series: str, number: str, ez: str) -> str:
    series_u = "T28" if series.casefold() == _T28 else series.upper()
    return f"{series_u}-{number}{ez.upper()}"


def _looks_like_content_id(direct: str) -> bool:
    return bool(direct) and direct[0].isdigit()


def _wide_numeric(direct: str) -> bool:
    return re.fullmatch(r"(?:t28|[a-z]+)\d{5,}[a-z]{0,3}", direct) is not None
