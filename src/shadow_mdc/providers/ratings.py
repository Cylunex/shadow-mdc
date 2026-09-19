"""Best-effort rating and short-review extraction from provider HTML/JSON."""

from __future__ import annotations

import re

from selectolax.parser import HTMLParser

from ..domain import ReviewHighlight

_RATING_SELECTORS = (
    ".d-review__average__num",
    ".d-review__average",
    "span.rating",
    ".rating .value",
    "[itemprop=ratingValue]",
    "p.d-review__average span",
)
_COUNT_SELECTORS = (
    ".d-review__evaluates span",
    ".d-review__evaluates",
    "[itemprop=reviewCount]",
    "span.count",
)
_REVIEW_UNIT_SELECTORS = (
    ".d-review__unit",
    ".review-list .review",
    "li.review",
)


def parse_hreview_rating(root: HTMLParser) -> tuple[float | None, int | None, float | None]:
    """Return (value, count, max) from FANZA/DMM-style review markup when present."""

    value: float | None = None
    for selector in _RATING_SELECTORS:
        node = root.css_first(selector)
        if node is None:
            continue
        raw = node.attributes.get("content") or node.text(separator=" ", strip=True)
        parsed = _parse_float(raw)
        if parsed is not None:
            value = parsed
            break
    count: int | None = None
    for selector in _COUNT_SELECTORS:
        node = root.css_first(selector)
        if node is None:
            continue
        raw = node.attributes.get("content") or node.text(separator=" ", strip=True)
        parsed_count = _parse_int(raw)
        if parsed_count is not None:
            count = parsed_count
            break
    rating_max = 5.0 if value is not None and value <= 5.5 else (10.0 if value is not None else None)
    return value, count, rating_max


def parse_short_reviews(root: HTMLParser, *, provider: str, limit: int = 5) -> tuple[ReviewHighlight, ...]:
    """Pull compact comment snippets; empty when none — never invent."""

    highlights: list[ReviewHighlight] = []
    for selector in _REVIEW_UNIT_SELECTORS:
        for unit in root.css(selector):
            title = _first_child_text(unit, (".d-review__unit__title", ".review-title", "h4", "strong"))
            comment = _first_child_text(
                unit,
                (".d-review__unit__comment", ".review-comment", ".review-body", "p"),
            )
            text = comment or title
            if not text or len(text) < 8:
                continue
            author = _first_child_text(unit, (".d-review__unit__reviewer", ".reviewer", ".author"))
            score_raw = _first_child_text(unit, (".d-review__unit__rating", ".rating"))
            highlights.append(
                ReviewHighlight(
                    provider=provider,
                    text=text[:500],
                    author=author,
                    score=_parse_float(score_raw),
                )
            )
            if len(highlights) >= limit:
                return tuple(highlights)
        if highlights:
            break
    return tuple(highlights)


def _first_child_text(node: object, selectors: tuple[str, ...]) -> str | None:
    for selector in selectors:
        child = node.css_first(selector)  # type: ignore[attr-defined]
        if child is None:
            continue
        value = child.text(separator=" ", strip=True)
        if value:
            return value
    return None


def _parse_float(raw: str | None) -> float | None:
    if not raw:
        return None
    match = re.search(r"(\d+(?:\.\d+)?)", raw.replace(",", ""))
    if match is None:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def _parse_int(raw: str | None) -> int | None:
    if not raw:
        return None
    match = re.search(r"(\d[\d,]*)", raw)
    if match is None:
        return None
    try:
        return int(match.group(1).replace(",", ""))
    except ValueError:
        return None
