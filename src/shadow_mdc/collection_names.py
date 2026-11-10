"""Collection helpers: kind detection and seed from Work string fields."""

from __future__ import annotations

import unicodedata

from .enums import CollectionKind
from .reference_models import catalog_rules


def normalize_collection_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def detect_collection_kind(name: str, *, preferred: CollectionKind | None = None) -> CollectionKind:
    """Map a free-text studio/series/label into a Collection kind.

    Known Chinese platforms (麻豆/探花/糖心…) stay PLATFORM even when the string
    came from a Work.series field, so the UI does not grow duplicate series+platform
    rows for the same brand.
    """

    if preferred is CollectionKind.LABEL:
        return CollectionKind.LABEL
    if preferred is CollectionKind.PLATFORM:
        return CollectionKind.PLATFORM
    rules = catalog_rules()
    key = normalize_collection_name(name)
    if key in rules.platform_names:
        return CollectionKind.PLATFORM
    # Allow short canonical platform stems inside longer studio strings.
    if any(stem in key for stem in rules.platform_stems):
        return CollectionKind.PLATFORM
    if preferred is CollectionKind.SERIES:
        return CollectionKind.SERIES
    if preferred is CollectionKind.STUDIO:
        return CollectionKind.STUDIO
    return preferred or CollectionKind.STUDIO


__all__ = [
    "detect_collection_kind",
    "normalize_collection_name",
]
