"""Serve actor portraits locally; never hand raw third-party CDN URLs to the UI.

``Actor.image_url`` may still hold a remote URL (provider CDN, GFriends/jsDelivr)
for rows that were never localized. Every API response maps such URLs through
:meth:`ActorImageCache.display_url`:

* a local copy already exists → ``/api/actor-images/remote-<key>.<ext>``;
* otherwise → ``/api/actor-images/remote/<key>``, which downloads the image on
  first view (validated real image bytes, size-capped), stores it under
  ``data/actor-images/`` and serves it. Failures return 404 — the browser never
  falls back to hotlinking.

Only URLs the catalog itself knows (DB actor rows) can be fetched, so the
endpoint is not an open proxy.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import httpx

from .gfriends_fill import _MIN_IMAGE_BYTES, _detect_image_ext

logger = logging.getLogger(__name__)

REMOTE_PREFIX = "remote-"
LOCAL_ROUTE = "/api/actor-images/"
LAZY_ROUTE = "/api/actor-images/remote/"
MAX_IMAGE_BYTES = 8 * 1024 * 1024
FAILURE_TTL_SECONDS = 30 * 60.0
_KEY_RE = re.compile(r"^[0-9a-f]{32}$")


def is_remote_url(value: str | None) -> bool:
    return bool(value) and str(value).strip().lower().startswith(("http://", "https://"))


def remote_key(url: str) -> str:
    return hashlib.sha256(url.strip().encode("utf-8")).hexdigest()[:32]


def valid_key(key: str) -> bool:
    return bool(_KEY_RE.match(key))


class ActorImageCache:
    def __init__(
        self,
        images_dir: Path | None,
        *,
        http: httpx.AsyncClient | None = None,
        url_source: Callable[[], Iterable[str]] | None = None,
        max_bytes: int = MAX_IMAGE_BYTES,
        failure_ttl: float = FAILURE_TTL_SECONDS,
    ) -> None:
        self._dir = images_dir
        self._http = http
        self._url_source = url_source
        self._max_bytes = max_bytes
        self._failure_ttl = failure_ttl
        self._index: dict[str, str] = {}
        self._failures: dict[str, float] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    # ----------------------------------------------------------------- lookup

    def local_file(self, key: str) -> Path | None:
        if self._dir is None or not valid_key(key) or not self._dir.is_dir():
            return None
        for candidate in self._dir.glob(f"{REMOTE_PREFIX}{key}.*"):
            if candidate.suffix in {".jpg", ".png", ".webp", ".gif"} and candidate.is_file():
                return candidate
        return None

    def display_url(self, url: str | None) -> str | None:
        """UI-safe URL for an actor image (local path, or the lazy-localize route)."""

        if not url or not url.strip():
            return None
        cleaned = url.strip()
        if not is_remote_url(cleaned):
            return cleaned
        key = remote_key(cleaned)
        self._index[key] = cleaned
        local = self.local_file(key)
        if local is not None:
            return f"{LOCAL_ROUTE}{local.name}"
        return f"{LAZY_ROUTE}{key}"

    def _resolve_url(self, key: str) -> str | None:
        url = self._index.get(key)
        if url is not None or self._url_source is None:
            return url
        # Restart / cached list response: rebuild the key → URL map from the catalog.
        for candidate in self._url_source():
            if is_remote_url(candidate):
                self._index[remote_key(candidate.strip())] = candidate.strip()
        return self._index.get(key)

    # --------------------------------------------------------------- localize

    async def localize(self, key: str) -> Path | None:
        """Return the local copy for ``key``, downloading it on first use."""

        if self._dir is None or not valid_key(key):
            return None
        local = self.local_file(key)
        if local is not None:
            return local
        failed_at = self._failures.get(key)
        if failed_at is not None and time.monotonic() - failed_at < self._failure_ttl:
            return None
        url = self._resolve_url(key)
        if url is None:
            return None
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            local = self.local_file(key)
            if local is not None:
                return local
            try:
                content = await self._download(url)
                extension = _detect_image_ext(content)
                if extension is None:
                    raise ValueError("not an image")
                path = self._write(key, extension, content)
            except (httpx.HTTPError, ValueError, OSError) as exc:
                logger.info("actor image localize failed key=%s: %s", key, type(exc).__name__)
                self._failures[key] = time.monotonic()
                return None
            self._failures.pop(key, None)
            return path

    async def _download(self, url: str) -> bytes:
        owns = self._http is None
        client = self._http or httpx.AsyncClient(timeout=30.0, follow_redirects=True)
        try:
            async with client.stream("GET", url, follow_redirects=True, timeout=30.0) as response:
                response.raise_for_status()
                chunks: list[bytes] = []
                total = 0
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > self._max_bytes:
                        raise ValueError("image too large")
                    chunks.append(chunk)
        finally:
            if owns:
                await client.aclose()
        content = b"".join(chunks)
        if len(content) < _MIN_IMAGE_BYTES:
            raise ValueError("image too small")
        return content

    def _write(self, key: str, extension: str, content: bytes) -> Path:
        assert self._dir is not None
        self._dir.mkdir(parents=True, exist_ok=True)
        target = self._dir / f"{REMOTE_PREFIX}{key}{extension}"
        descriptor, temporary = tempfile.mkstemp(prefix=f".{target.name}", suffix=".part", dir=self._dir)
        try:
            with os.fdopen(descriptor, "wb") as stream:
                stream.write(content)
            os.replace(temporary, target)
        except Exception:
            Path(temporary).unlink(missing_ok=True)
            raise
        return target


_default = ActorImageCache(None)


def configure_actor_images(cache: ActorImageCache) -> None:
    global _default
    _default = cache


def actor_images() -> ActorImageCache:
    return _default


def actor_image_display_url(url: str | None) -> str | None:
    return _default.display_url(url)
