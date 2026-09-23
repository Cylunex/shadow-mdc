"""Actress name alias expansion (romaji / CJK variants → Japanese).

Used to raise GFriends portrait match rate when catalog actors are stored
under English/romaji names while Filetree keys are Japanese.
"""

from __future__ import annotations

import gzip
import json
import re
import threading
import unicodedata
from functools import lru_cache
from importlib import resources
from pathlib import Path

_WHITESPACE = re.compile(r"\s+")


def normalize_actress_key(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return _WHITESPACE.sub("", normalized)


class ActressNameMap:
    """Lazy-loaded alias → Japanese display-name table."""

    def __init__(self, mapping: dict[str, str] | None = None) -> None:
        self._mapping = {normalize_actress_key(k): v for k, v in (mapping or {}).items() if k and v}
        self._lock = threading.Lock()

    @classmethod
    def from_mapping(cls, mapping: dict[str, str]) -> ActressNameMap:
        return cls(mapping)

    @classmethod
    def load_default(cls) -> ActressNameMap:
        return cls(_load_bundled_map())

    @classmethod
    def load_path(cls, path: Path) -> ActressNameMap:
        raw = Path(path).read_bytes()
        if str(path).endswith(".gz"):
            raw = gzip.decompress(raw)
        payload = json.loads(raw.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("actress name map must be a JSON object")
        return cls({str(k): str(v) for k, v in payload.items()})

    def __len__(self) -> int:
        return len(self._mapping)

    def resolve_japanese(self, name: str) -> str | None:
        key = normalize_actress_key(name)
        if not key:
            return None
        return self._mapping.get(key)

    def expand_candidates(self, names: list[str] | tuple[str, ...]) -> list[str]:
        """Return unique candidate display names, JP aliases first when known."""

        ordered: list[str] = []
        seen: set[str] = set()

        def _add(value: str) -> None:
            cleaned = value.strip()
            if not cleaned:
                return
            key = normalize_actress_key(cleaned)
            if not key or key in seen:
                return
            seen.add(key)
            ordered.append(cleaned)

        # Prefer Japanese forms first — GFriends Filetree is JP-centric.
        for name in names:
            japanese = self.resolve_japanese(name)
            if japanese:
                _add(japanese)
        for name in names:
            _add(name)
            # Also try swapped "Given Family" ↔ "Family Given" for ASCII names.
            parts = name.strip().split()
            if len(parts) == 2 and all(part.isascii() for part in parts):
                swapped = f"{parts[1]} {parts[0]}"
                japanese = self.resolve_japanese(swapped)
                if japanese:
                    _add(japanese)
                _add(swapped)
        return ordered


_default_map: ActressNameMap | None = None
_default_lock = threading.Lock()


def get_actress_name_map() -> ActressNameMap:
    global _default_map
    if _default_map is not None:
        return _default_map
    with _default_lock:
        if _default_map is None:
            _default_map = ActressNameMap.load_default()
        return _default_map


@lru_cache(maxsize=1)
def _load_bundled_map() -> dict[str, str]:
    package = resources.files("shadow_mdc.data")
    resource = package.joinpath("actress_name_map.json.gz")
    with resources.as_file(resource) as path:
        raw = gzip.decompress(Path(path).read_bytes())
    payload = json.loads(raw.decode("utf-8"))
    if not isinstance(payload, dict):
        return {}
    return {str(k): str(v) for k, v in payload.items()}
