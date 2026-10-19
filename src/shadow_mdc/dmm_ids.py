"""DMM content_id candidate expansion.

Ideas from ShotHeadman/mdcz ``contentId.ts``; the per-series maker-prefix table
(``data/dmm_content_id_prefixes.json.gz``, e.g. ``abf → 118/436`` so ``ABF-387``
→ ``118abf00387``) is ported from javinizer/javinizer-go
``internal/r18devdump/content_id_prefixes.go`` (MIT; generated from the CC0
r18.dev dump). Marker-aware peel/re-append follows javinizer-go
``ContentIDCandidatesWithMarker`` (MIT).
"""

from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path

from .remaster import parse_remaster_identity

_CODE_PARTS = re.compile(r"(?i)(\d*[a-z]+)[-_]?(\d+)")
_SERIES_DIGITS = re.compile(r"^(\d*)([a-z]+)$")
# PPV partner content ids (``h_086mesu00103``) are already canonical.
_UNDERSCORE_CONTENT_ID = re.compile(r"^[a-z]_\d+[a-z]+\d+[a-z]?$")
_HYPHEN_SERIES = re.compile(r"(?i)^(t28|[a-z]{1,10})-(\d+)([ez])?$")
_T28 = "t28"
_WIDE_NUMERIC_CID = re.compile(r"^(?:t28|[a-z]+)\d{5,}[a-z]{0,3}$")


@lru_cache(maxsize=1)
def _prefix_table() -> dict[str, tuple[str, ...]]:
    try:
        resource = resources.files("shadow_mdc.data").joinpath("dmm_content_id_prefixes.json.gz")
        with resources.as_file(resource) as path:
            payload = json.loads(gzip.decompress(Path(path).read_bytes()).decode("utf-8"))
    except (OSError, ValueError, ModuleNotFoundError):
        return {}
    if not isinstance(payload, dict):
        return {}
    return {
        str(series): tuple(str(item) for item in prefixes if isinstance(item, str))
        for series, prefixes in payload.items()
        if isinstance(prefixes, list)
    }


def known_content_id_prefixes(series: str) -> tuple[str, ...]:
    """Known DMM maker prefixes for a lowercase series (``""`` = no prefix)."""

    return _prefix_table().get(series.strip().casefold(), ())


def _prefix_rank(prefix: str) -> tuple[int, int, str]:
    # Plain and "1" prefixes are by far the most common DMM digital forms; then
    # short numeric maker ids; the h_/n_ partner prefixes last.
    if prefix == "":
        return (0, 0, prefix)
    if prefix == "1":
        return (1, 0, prefix)
    if prefix.isdigit():
        return (2, len(prefix), prefix)
    return (3, len(prefix), prefix)


def content_id_candidates(value: str, *, limit: int | None = None) -> list[str]:
    """Return plausible DMM content_id spellings for a DVD / 番号 string.

    When the input carries a remaster marker (H/HD/AI), candidates are built
    from the base id and the folded marker is re-appended. Known maker prefixes
    come first (5-digit padding, then 3-digit / raw), then the generic
    ``1<series>`` / ``<series>`` forms. ``limit`` caps the list for callers
    that pay one HTTP request per candidate.
    """

    if parse_remaster_identity(value) is not None:
        return content_id_candidates_with_marker(value, limit=limit)
    return _expand_base_candidates(value, limit=limit)


def content_id_candidates_with_marker(value: str, *, limit: int | None = None) -> list[str]:
    """Peel H/HD/AI, expand base candidates, re-append folded marker (+ optional E/Z)."""

    remaster = parse_remaster_identity(value)
    if remaster is None:
        return _expand_base_candidates(value, limit=limit)

    raw = value.strip().casefold()
    compacted = raw.translate(str.maketrans("", "", "-_. "))
    out: list[str] = []
    seen: set[str] = set()

    def add(item: str) -> None:
        if item and item not in seen:
            seen.add(item)
            out.append(item)

    # Raw / channel-prefixed content ids keep literal identity leading.
    if not remaster.had_separator and (
        (compacted[:1].isdigit() and compacted[0].isdigit()) or _WIDE_NUMERIC_CID.match(compacted)
    ):
        add(compacted)

    suffix = remaster.catalog_suffix.casefold()
    marker = remaster.content_marker
    base = _expand_base_candidates(remaster.base, limit=None)
    for candidate in base:
        add(f"{candidate}{suffix}{marker}")
    return out[:limit] if limit is not None else out


def _expand_base_candidates(value: str, *, limit: int | None = None) -> list[str]:
    normalized = value.strip().lower()
    if not normalized:
        return []
    if _UNDERSCORE_CONTENT_ID.match(normalized):
        return [normalized]

    # Hyphenated display ids pin the series boundary (T28-557 → t28/557, not t/28).
    hyphen = _HYPHEN_SERIES.fullmatch(normalized.strip())
    if hyphen is not None:
        series = hyphen.group(1).casefold()
        digits = hyphen.group(2)
        ez = (hyphen.group(3) or "").casefold()
        return _candidates_for_series_number(
            series,
            digits,
            limit=limit,
            catalog_suffix=ez,
            raw_prefix=None,
            verbatim_prefix=None,
        )

    matched = _CODE_PARTS.search(normalized)
    if matched is None:
        fallback = re.sub(r"[^a-z0-9]", "", normalized)
        return [fallback] if fallback else []

    raw_prefix, digits = matched.group(1), matched.group(2)
    compact_stem = re.sub(r"[^a-z0-9]", "", normalized)
    t28_full = re.fullmatch(r"(t28)(\d+)", compact_stem)
    if t28_full is not None:
        return _candidates_for_series_number(
            _T28,
            t28_full.group(2),
            limit=limit,
            catalog_suffix="",
            raw_prefix=None,
            verbatim_prefix=None,
            prefix_override=("9", "", "1"),
        )

    prefix = raw_prefix[1:] if raw_prefix.startswith("1") and len(raw_prefix) > 1 else raw_prefix
    series_match = _SERIES_DIGITS.match(raw_prefix)
    series = series_match.group(2) if series_match else prefix
    verbatim_prefix = None
    channel_digits = series_match.group(1) if series_match else ""
    if channel_digits and (not raw_prefix.startswith("1") or len(channel_digits) > 1):
        prefix = series
        verbatim_prefix = raw_prefix
    leading_one = raw_prefix if raw_prefix.startswith("1") else None
    return _candidates_for_series_number(
        series,
        digits,
        limit=limit,
        catalog_suffix="",
        raw_prefix=prefix,
        verbatim_prefix=verbatim_prefix,
        leading_one_prefix=leading_one,
    )


def _candidates_for_series_number(
    series: str,
    digits: str,
    *,
    limit: int | None,
    catalog_suffix: str,
    raw_prefix: str | None,
    verbatim_prefix: str | None,
    leading_one_prefix: str | None = None,
    prefix_override: tuple[str, ...] | None = None,
) -> list[str]:
    series = series.casefold()
    padded = digits.zfill(5)
    padded3 = str(int(digits)).zfill(3) if digits.isdigit() else digits
    # Catalog E/Z (rare on content_id) rides after the number digits.
    suffix = catalog_suffix.casefold()
    candidates: list[str] = []
    if leading_one_prefix:
        candidates += [
            f"{leading_one_prefix}{padded}{suffix}",
            f"{leading_one_prefix}{digits}{suffix}",
        ]
    if verbatim_prefix:
        candidates += [
            f"{verbatim_prefix}{padded}{suffix}",
            f"{verbatim_prefix}{digits}{suffix}",
        ]

    if prefix_override is not None:
        known: tuple[str, ...] = prefix_override
    elif series == _T28:
        known = ("9", "", "1")
    else:
        known = tuple(sorted(known_content_id_prefixes(series), key=_prefix_rank))

    for maker in known:
        candidates.append(f"{maker}{series}{padded}{suffix}")
    for maker in known:
        candidates.append(f"{maker}{series}{padded3}{suffix}")
        candidates.append(f"{maker}{series}{digits}{suffix}")

    prefix = raw_prefix if raw_prefix is not None else series
    if prefix_override is None:
        candidates += [
            f"1{prefix}{padded}{suffix}",
            f"{prefix}{padded}{suffix}",
            f"1{prefix}{digits}{suffix}",
            f"{prefix}{digits}{suffix}",
        ]
    unique = list(dict.fromkeys(item for item in candidates if item))
    return unique[:limit] if limit is not None else unique
