"""GFriends actress image index (Filetree.json).

Adapted from tinypinglite/sakuramediabe ``metadata/gfriends.py`` and the
lookup shape used by Emby.Plugins.JavScraper ``Scrapers/Gfriends.cs``.
Image bytes live on the gfriends/gfriends GitHub repo; we only cache the
JSON index and resolve names → CDN URLs (or download separately).
"""

from __future__ import annotations

import json
import re
import tempfile
import threading
import time
import unicodedata
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlsplit, urlunsplit

import httpx

DEFAULT_FILETREE_URL = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/Filetree.json"
DEFAULT_CDN_BASE_URL = "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master"
_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".webp"})
_DISK_RECHECK_INTERVAL_SECONDS = 60.0


def normalize_gfriends_name(value: str) -> str:
    """NFKC + casefold + strip all whitespace (matches sakuramediabe)."""

    normalized = unicodedata.normalize("NFKC", value or "")
    normalized = normalized.strip().casefold()
    return re.sub(r"\s+", "", normalized)


class GfriendsActorImageResolver:
    """Resolve actress names to portrait URLs via a cached Filetree index."""

    FILETREE_REQUEST_TIMEOUT = 60.0

    def __init__(
        self,
        *,
        filetree_url: str = DEFAULT_FILETREE_URL,
        cdn_base_url: str = DEFAULT_CDN_BASE_URL,
        cache_path: Path,
        cache_ttl_hours: int = 24 * 7,
        client: httpx.Client | None = None,
    ) -> None:
        self.filetree_url = filetree_url
        self.cdn_base_url = cdn_base_url.rstrip("/")
        self.cache_path = Path(cache_path).expanduser()
        if not self.cache_path.is_absolute():
            self.cache_path = (Path.cwd() / self.cache_path).resolve()
        self.cache_ttl_seconds = max(cache_ttl_hours, 1) * 3600
        self._client = client
        self._owns_client = client is None
        self._index: dict[str, str] | None = None
        self._disk_hydrated = False
        self._hydrated_mtime: float | None = None
        self._next_disk_check = 0.0
        self._lock = threading.Lock()

    def close(self) -> None:
        if self._owns_client and self._client is not None:
            self._client.close()
            self._client = None

    def resolve(self, candidate_names: list[str] | tuple[str, ...]) -> str | None:
        """Return CDN URL for the first matching name; never hits the network."""

        index = self._read_index_lazy()
        if not index:
            return None
        for candidate_name in candidate_names:
            key = normalize_gfriends_name(candidate_name)
            if not key:
                continue
            relative = index.get(key)
            if relative:
                return self._cdn_url(relative)
        return None

    def refresh(self, *, force: bool = False) -> dict[str, Any]:
        """Fetch Filetree (or reuse fresh disk cache) and rebuild the memory index."""

        with self._lock:
            if not force and self._is_cache_fresh():
                self._hydrate_from_disk_locked()
                return {
                    "entries": len(self._index or {}),
                    "source": "cache_fresh",
                    "bytes_written": 0,
                    "force": force,
                }

            try:
                payload = self._fetch_filetree()
            except Exception:
                self._hydrate_from_disk_locked()
                if self._index:
                    return {
                        "entries": len(self._index),
                        "source": "stale_cache",
                        "bytes_written": 0,
                        "force": force,
                    }
                raise

            bytes_written = self._write_cache_payload(payload)
            self._index = self._build_index(payload)
            self._disk_hydrated = True
            self._hydrated_mtime = self._cache_mtime()
            return {
                "entries": len(self._index),
                "source": "network",
                "bytes_written": bytes_written,
                "force": force,
            }

    def _cdn_url(self, relative_path: str) -> str:
        cleaned = relative_path.lstrip("/")
        # Encode path segments but keep slashes; strip accidental query fragments.
        path_only = cleaned.split("?", 1)[0]
        encoded = "/".join(quote(part, safe="") for part in path_only.split("/") if part != "")
        return f"{self.cdn_base_url}/{encoded}"

    def _http(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.FILETREE_REQUEST_TIMEOUT,
                headers={"User-Agent": "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc; gfriends)"},
                follow_redirects=True,
            )
        return self._client

    def _fetch_filetree(self) -> Any:
        response = self._http().get(self.filetree_url)
        response.raise_for_status()
        return response.json()

    def _read_index_lazy(self) -> dict[str, str]:
        now_ts = time.time()
        if now_ts >= self._next_disk_check:
            with self._lock:
                if now_ts >= self._next_disk_check:
                    self._next_disk_check = now_ts + _DISK_RECHECK_INTERVAL_SECONDS
                    if self._cache_mtime() != self._hydrated_mtime:
                        self._index = None
                        self._disk_hydrated = False
        if self._index is not None:
            return self._index
        if self._disk_hydrated:
            return {}
        with self._lock:
            if self._index is not None:
                return self._index
            if self._disk_hydrated:
                return {}
            self._hydrate_from_disk_locked()
            return self._index or {}

    def _hydrate_from_disk_locked(self) -> None:
        self._hydrated_mtime = self._cache_mtime()
        if self._index:
            self._disk_hydrated = True
            return
        payload = self._read_cache_payload()
        self._disk_hydrated = True
        if payload is None:
            return
        self._index = self._build_index(payload)

    def _cache_mtime(self) -> float | None:
        try:
            return self.cache_path.stat().st_mtime
        except OSError:
            return None

    def _is_cache_fresh(self) -> bool:
        if not self.cache_path.exists():
            return False
        age_seconds = time.time() - self.cache_path.stat().st_mtime
        return age_seconds <= self.cache_ttl_seconds

    def _read_cache_payload(self) -> Any | None:
        if not self.cache_path.exists():
            return None
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None

    def _write_cache_payload(self, payload: Any) -> int:
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        content = json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=str(self.cache_path.parent),
            delete=False,
        ) as temp_file:
            temp_file.write(content)
            temp_path = Path(temp_file.name)
        temp_path.replace(self.cache_path)
        return len(content.encode("utf-8"))

    def _build_index(self, payload: Any) -> dict[str, str]:
        index: dict[str, str] = {}
        for display_name, relative_path in self._extract_file_entries(payload):
            key = normalize_gfriends_name(Path(display_name).stem)
            if not key or key in index:
                continue
            index[key] = relative_path
        return index

    def _extract_file_entries(self, payload: Any) -> list[tuple[str, str]]:
        if isinstance(payload, dict) and isinstance(payload.get("Content"), dict):
            return self._extract_content_mapping_entries(payload["Content"], ["Content"])
        return []

    def _extract_content_mapping_entries(self, node: Any, path_parts: list[str]) -> list[tuple[str, str]]:
        entries: list[tuple[str, str]] = []
        if not isinstance(node, dict):
            return entries
        for key, value in node.items():
            if isinstance(value, str):
                # Filetree values often look like ``AI-Fix-name.jpg?t=123``; keep file only.
                file_name = value.split("?", 1)[0].lstrip("/")
                relative_path = "/".join([*path_parts, file_name])
                extension = Path(key).suffix.lower()
                if extension in _IMAGE_EXTENSIONS:
                    entries.append((key, relative_path))
                continue
            entries.extend(self._extract_content_mapping_entries(value, [*path_parts, key]))
        return entries


def rewrite_cdn_base(url: str, new_base: str) -> str:
    """Replace the scheme/host(/gh prefix) of a resolved gfriends URL (tests/helpers)."""

    parts = urlsplit(url)
    base = urlsplit(new_base.rstrip("/") + "/")
    path = base.path.rstrip("/") + parts.path
    return urlunsplit((base.scheme, base.netloc, path, parts.query, parts.fragment))
