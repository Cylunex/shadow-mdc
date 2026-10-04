"""DMM content_id candidate expansion.

Ideas from ShotHeadman/mdcz ``contentId.ts``; the per-series maker-prefix table
(``data/dmm_content_id_prefixes.json.gz``, e.g. ``abf → 118/436`` so ``ABF-387``
→ ``118abf00387``) is ported from javinizer/javinizer-go
``internal/r18devdump/content_id_prefixes.go`` (MIT; generated from the CC0
r18.dev dump).
"""

from __future__ import annotations

import gzip
import json
import re
from functools import lru_cache
from importlib import resources
from pathlib import Path

_CODE_PARTS = re.compile(r"(?i)(\d*[a-z]+)[-_]?(\d+)")
_SERIES_DIGITS = re.compile(r"^(\d*)([a-z]+)$")
# PPV partner content ids (``h_086mesu00103``) are already canonical.
_UNDERSCORE_CONTENT_ID = re.compile(r"^[a-z]_\d+[a-z]+\d+[a-z]?$")


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

    Known maker prefixes come first (5-digit padding, then 3-digit / raw), then the
    generic ``1<series>`` / ``<series>`` forms. ``limit`` caps the list for callers
    that pay one HTTP request per candidate.
    """

    normalized = value.strip().lower()
    if not normalized:
        return []
    if _UNDERSCORE_CONTENT_ID.match(normalized):
        return [normalized]
    matched = _CODE_PARTS.search(normalized)
    if matched is None:
        fallback = re.sub(r"[^a-z0-9]", "", normalized)
        return [fallback] if fallback else []

    raw_prefix, digits = matched.group(1), matched.group(2)
    prefix = raw_prefix[1:] if raw_prefix.startswith("1") and len(raw_prefix) > 1 else raw_prefix
    padded = digits.zfill(5)
    padded3 = str(int(digits)).zfill(3) if digits.isdigit() else digits
    candidates: list[str] = []
    if raw_prefix.startswith("1"):
        candidates += [f"{raw_prefix}{padded}", f"{raw_prefix}{digits}"]

    series_match = _SERIES_DIGITS.match(raw_prefix)
    series = series_match.group(2) if series_match else prefix
    if series_match and series_match.group(1) and not raw_prefix.startswith("1"):
        prefix = series
    elif series_match and len(series_match.group(1)) > 1:
        prefix = series
    if series_match and series_match.group(1) and not raw_prefix.startswith("1"):
        # Already content-id shaped (``118abf00387``): keep it verbatim first.
        candidates += [f"{raw_prefix}{padded}", f"{raw_prefix}{digits}"]
    known = sorted(known_content_id_prefixes(series), key=_prefix_rank)
    for maker in known:
        candidates.append(f"{maker}{series}{padded}")
    for maker in known:
        candidates.append(f"{maker}{series}{padded3}")
        candidates.append(f"{maker}{series}{digits}")

    candidates += [
        f"1{prefix}{padded}",
        f"{prefix}{padded}",
        f"1{prefix}{digits}",
        f"{prefix}{digits}",
    ]
    unique = list(dict.fromkeys(item for item in candidates if item))
    return unique[:limit] if limit is not None else unique
