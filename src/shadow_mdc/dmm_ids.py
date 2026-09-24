"""DMM content_id candidate expansion (ideas from ShotHeadman/mdcz contentId.ts)."""

from __future__ import annotations

import re

_CODE_PARTS = re.compile(r"(?i)(\d*[a-z]+)[-_]?(\d+)")


def content_id_candidates(value: str) -> list[str]:
    """Return plausible DMM content_id spellings for a DVD / 番号 string."""

    normalized = value.strip().lower()
    if not normalized:
        return []
    matched = _CODE_PARTS.search(normalized)
    if matched is None:
        fallback = re.sub(r"[^a-z0-9]", "", normalized)
        return [fallback] if fallback else []

    raw_prefix, digits = matched.group(1), matched.group(2)
    prefix = raw_prefix[1:] if raw_prefix.startswith("1") and len(raw_prefix) > 1 else raw_prefix
    padded = digits.zfill(5)
    candidates = [
        f"1{prefix}{padded}",
        f"{prefix}{padded}",
        f"1{prefix}{digits}",
        f"{prefix}{digits}",
    ]
    if raw_prefix.startswith("1"):
        candidates[0:0] = [f"{raw_prefix}{padded}", f"{raw_prefix}{digits}"]
    return list(dict.fromkeys(item for item in candidates if item))
