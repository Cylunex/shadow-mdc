"""Lightweight API response cache with Redis backend and in-process TTL fallback.

When ``SHADOW_MDC_REDIS_URL`` / settings.redis_url is set and the ``redis`` package
is available, values are stored in Redis. Otherwise an in-process dict with TTL is
used so tests and local dev never hard-depend on Redis.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# TTL constants (seconds)
TTL_STATIC = 60 * 60 * 24 * 30  # ~30 days — yearly TOP / rankings until invalidate
TTL_COLLECTIONS = 60 * 60 * 24 * 7  # 7 days
TTL_TAGS = 60 * 10  # 10 minutes
TTL_ACTORS = 60 * 15  # 15 minutes

KEY_PREFIX = "shadow_mdc:api:"


@dataclass(frozen=True)
class CacheStats:
    backend: str
    hits: int = 0
    misses: int = 0


class _MemoryBackend:
    def __init__(self) -> None:
        self._store: dict[str, tuple[float | None, str]] = {}
        self._lock = threading.RLock()

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, payload = entry
            if expires_at is not None and expires_at <= time.monotonic():
                self._store.pop(key, None)
                return None
            return payload

    def set(self, key: str, value: str, ttl_seconds: int | None) -> None:
        expires_at: float | None
        if ttl_seconds is None or ttl_seconds <= 0:
            expires_at = None
        else:
            expires_at = time.monotonic() + ttl_seconds
        with self._lock:
            self._store[key] = (expires_at, value)

    def delete(self, key: str) -> None:
        with self._lock:
            self._store.pop(key, None)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            victims = [key for key in self._store if key.startswith(prefix)]
            for key in victims:
                self._store.pop(key, None)
            return len(victims)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()


class _RedisBackend:
    def __init__(self, url: str) -> None:
        import redis  # type: ignore[import-untyped]

        self._client = redis.Redis.from_url(url, decode_responses=True)
        # Fail fast if unreachable so we can fall back to memory.
        self._client.ping()

    def get(self, key: str) -> str | None:
        value = self._client.get(key)
        return value if isinstance(value, str) else None

    def set(self, key: str, value: str, ttl_seconds: int | None) -> None:
        if ttl_seconds is None or ttl_seconds <= 0:
            self._client.set(key, value)
        else:
            self._client.setex(key, int(ttl_seconds), value)

    def delete(self, key: str) -> None:
        self._client.delete(key)

    def delete_prefix(self, prefix: str) -> int:
        deleted = 0
        cursor = 0
        pattern = f"{prefix}*"
        while True:
            cursor, keys = self._client.scan(cursor=cursor, match=pattern, count=200)
            if keys:
                deleted += int(self._client.delete(*keys))
            if cursor == 0:
                break
        return deleted

    def clear(self) -> None:
        self.delete_prefix(KEY_PREFIX)


class ResponseCache:
    """JSON get/set cache shared across API handlers."""

    def __init__(self, redis_url: str | None = None) -> None:
        self._hits = 0
        self._misses = 0
        self._lock = threading.Lock()
        self._backend: _MemoryBackend | _RedisBackend
        self._backend_name: str
        url = (redis_url or "").strip() or None
        if url:
            try:
                self._backend = _RedisBackend(url)
                self._backend_name = "redis"
                logger.info("response cache using Redis at %s", _safe_redis_host(url))
            except Exception as exc:  # noqa: BLE001 — soft fallback
                logger.warning("Redis unavailable (%s); using in-process cache", exc)
                self._backend = _MemoryBackend()
                self._backend_name = "memory"
        else:
            self._backend = _MemoryBackend()
            self._backend_name = "memory"

    @property
    def backend(self) -> str:
        return self._backend_name

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(backend=self._backend_name, hits=self._hits, misses=self._misses)

    def get_json(self, key: str) -> Any | None:
        full = self._full_key(key)
        raw = self._backend.get(full)
        if raw is None:
            with self._lock:
                self._misses += 1
            return None
        with self._lock:
            self._hits += 1
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self._backend.delete(full)
            return None

    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = TTL_STATIC) -> None:
        full = self._full_key(key)
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        self._backend.set(full, payload, ttl_seconds)

    def invalidate(self, *keys: str) -> None:
        for key in keys:
            self._backend.delete(self._full_key(key))

    def invalidate_prefix(self, prefix: str) -> int:
        return self._backend.delete_prefix(self._full_key(prefix))

    def clear(self) -> None:
        self._backend.clear()
        with self._lock:
            self._hits = 0
            self._misses = 0

    def get_or_set(
        self,
        key: str,
        factory: Callable[[], T],
        *,
        ttl_seconds: int | None = TTL_STATIC,
        serialize: Callable[[T], Any] | None = None,
    ) -> T:
        """Return cached value or compute + store.

        ``serialize`` converts the live object to a JSON-friendly form for storage.
        On cache hit the JSON form is returned (caller should re-hydrate if needed).
        Prefer ``cached_model`` helpers in api.py for Pydantic models.
        """
        cached = self.get_json(key)
        if cached is not None:
            return cached  # type: ignore[return-value]
        value = factory()
        to_store = serialize(value) if serialize is not None else value
        self.set_json(key, to_store, ttl_seconds=ttl_seconds)
        return value

    @staticmethod
    def _full_key(key: str) -> str:
        if key.startswith(KEY_PREFIX):
            return key
        return f"{KEY_PREFIX}{key}"


def _safe_redis_host(url: str) -> str:
    try:
        from urllib.parse import urlparse

        parsed = urlparse(url)
        host = parsed.hostname or "?"
        port = parsed.port or 6379
        return f"{host}:{port}"
    except Exception:  # noqa: BLE001
        return "<redis>"


# Convenience key builders
def collections_list_key(*, kind: str | None, q: str | None) -> str:
    return f"collections:list:kind={kind or ''}:q={q or ''}"


def collection_detail_key(collection_id: str) -> str:
    return f"collections:detail:{collection_id}"


def javranking_sections_key() -> str:
    return "javranking:sections"


def javranking_list_key(slug: str) -> str:
    return f"javranking:list:{slug}"


def works_tags_key(limit: int) -> str:
    return f"works:tags:limit={limit}"


def actors_list_key() -> str:
    return "actors:list"
