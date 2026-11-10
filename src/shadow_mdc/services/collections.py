"""Compat re-export; helpers live in shadow_mdc.collection_names."""
from ..collection_names import (
    detect_collection_kind,
    normalize_collection_name,
)

__all__ = ["detect_collection_kind", "normalize_collection_name"]
