"""Compat re-export; helpers live in shadow_mdc.collection_names."""
from ..collection_names import (
    KNOWN_PLATFORM_NAMES,
    detect_collection_kind,
    normalize_collection_name,
)

__all__ = ["KNOWN_PLATFORM_NAMES", "detect_collection_kind", "normalize_collection_name"]
