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


_TOKEN_RUNS = re.compile(r"[A-Z]+|\d+", re.IGNORECASE)


def is_format_equivalent(left: str | None, right: str | None) -> bool:
    """Padding-tolerant catalogue format match (same letter/digit runs after zero-strip).

    ``ABC-001`` ≡ ``ABC001`` ≡ ``abc-1``. Letter serial variants stay distinct
    (``FJIN-106`` ≢ ``FJIN-106A``). Inspired by peer scan semantics; original
    implementation not copied.
    """

    if not left or not right:
        return False
    return _format_key(left) == _format_key(right)


def _format_key(raw: str) -> str:
    text = unicodedata.normalize("NFKC", raw).strip().upper()
    text = _FC2_PPV.sub("FC2-", text)
    parts: list[str] = []
    for token in _TOKEN_RUNS.findall(text):
        if token.isdigit():
            parts.append(str(int(token)))
        else:
            parts.append(token.upper())
    return "-".join(parts)
