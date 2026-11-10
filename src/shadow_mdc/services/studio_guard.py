"""Reject non-JAV studio/label/fuzzy names when creating or importing actors.

Mirrors cleanup_non_jav_mis_tagged_actors patterns (麻豆/探花/传媒/制片厂…).
"""

from __future__ import annotations

import re
import unicodedata

from ..reference_models import catalog_rules

_FUZZY_EXACT = re.compile(
    r"^(?:未知|素人|匿名|不明|暂无|无|女优|演员|演员\d+|女优\d+|\d+)$",
    re.UNICODE,
)


def normalize_actor_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def classify_non_jav_actor_rejection(name: str) -> str | None:
    """Return rejection reason (`studio_label` / `fuzzy`) or None if allowed."""

    rules = catalog_rules()
    cleaned = name.strip()
    if not cleaned:
        return "fuzzy"
    if cleaned in rules.keep_borderline:
        return None
    if cleaned in rules.explicit_fuzzy_names or _FUZZY_EXACT.fullmatch(cleaned):
        return "fuzzy"
    if cleaned in rules.explicit_studio_labels:
        return "studio_label"
    for prefix in rules.prefix_series:
        if cleaned.startswith(prefix) and cleaned != prefix:
            return "studio_label"
    for suffix in rules.suffix_series:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return "studio_label"
    for token in rules.studio_tokens:
        if token in cleaned and cleaned.endswith(token):
            return "studio_label"
    return None


def reject_non_jav_studio_label(name: str) -> str | None:
    """Human-readable rejection detail, or None when allowed."""

    reason = classify_non_jav_actor_rejection(name)
    if reason is None:
        return None
    if reason == "studio_label":
        return f"拒绝将片商/工作室标签「{name.strip()}」登记为演员"
    return f"拒绝模糊/占位名称「{name.strip()}」登记为演员"
