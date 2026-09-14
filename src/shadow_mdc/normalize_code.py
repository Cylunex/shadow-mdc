"""Python port of javranking-extension normalizeCode / toComparisonKey."""

from __future__ import annotations

import re
import unicodedata

_FC2_PPV = re.compile(r"\bFC2[-_\s]*PPV[-_\s]*", re.IGNORECASE)
# space, underscore, em dash, en dash, hyphen
_SEPARATORS_TO_DASH = re.compile(r"[\s_\u2014\u2013-]+")
_SEPARATORS_STRIP = re.compile(r"[\s_\u2014\u2013-]+")


def normalize_code(raw: str | None) -> str:
    """Normalize a video code (NFKC, upper, FC2-PPV, dash/underscore/space runs).

    Mirrors ``normalizeCode`` from aizhimou/javranking-extension.
    """

    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw).strip().upper()
    text = _FC2_PPV.sub("FC2-", text)
    text = _SEPARATORS_TO_DASH.sub("-", text)
    return text


def to_comparison_key(raw: str | None) -> str:
    """Comparison key with separators removed (letters/digits preserved).

    Mirrors ``toComparisonKey`` from aizhimou/javranking-extension.
    """

    if not raw:
        return ""
    text = unicodedata.normalize("NFKC", raw).strip().upper()
    text = _FC2_PPV.sub("FC2-", text)
    text = _SEPARATORS_STRIP.sub("", text)
    return text
