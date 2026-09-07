"""Collection helpers: kind detection and seed from Work string fields."""

from __future__ import annotations

import unicodedata

from .enums import CollectionKind

# Chinese-first platforms / studios that should be treated as platform rather than loose tags.
KNOWN_PLATFORM_NAMES = frozenset(
    {
        "麻豆",
        "麻豆传媒",
        "madou",
        "探花",
        "91探花",
        "ktv探花",
        "糖心",
        "糖心vlog",
        "天美传媒",
        "果冻传媒",
        "星空无限传媒",
        "蜜桃影像",
        "蜜桃影像传媒",
        "皇家华人",
        "杏吧",
        "杏吧传媒",
        "swag",
        "onlyfans",
        "hongkongdoll",
        "91制片厂",
        "吃瓜传媒",
        "乌鸦传媒",
        "大象传媒",
    }
)


def normalize_collection_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def detect_collection_kind(name: str, *, preferred: CollectionKind | None = None) -> CollectionKind:
    """Map a free-text studio/series/label into a Collection kind."""

    if preferred is CollectionKind.SERIES:
        return CollectionKind.SERIES
    if preferred is CollectionKind.LABEL:
        return CollectionKind.LABEL
    if preferred is CollectionKind.PLATFORM:
        return CollectionKind.PLATFORM
    key = normalize_collection_name(name)
    if key in KNOWN_PLATFORM_NAMES:
        return CollectionKind.PLATFORM
    # Allow short canonical platform stems inside longer studio strings.
    if any(stem in key for stem in ("麻豆", "探花", "糖心", "天美", "果冻", "星空无限")):
        return CollectionKind.PLATFORM
    if preferred is CollectionKind.STUDIO:
        return CollectionKind.STUDIO
    return preferred or CollectionKind.STUDIO


__all__ = [
    "KNOWN_PLATFORM_NAMES",
    "detect_collection_kind",
    "normalize_collection_name",
]
