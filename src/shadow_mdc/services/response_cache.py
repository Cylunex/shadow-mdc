"""Lightweight API response cache with Redis backend and in-process TTL fallback.

When ``settings.redis_url`` is set, values are stored in Redis. Connection failures
at startup or runtime switch to a bounded in-process TTL cache, so Redis remains
optional for tests and local development.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, TypeVar, cast
from urllib.parse import urlparse

import redis

logger = logging.getLogger(__name__)

T = TypeVar("T")

# TTL constants (seconds)
TTL_STATIC = 60 * 60 * 24 * 30  # ~30 days — yearly TOP / rankings until invalidate
TTL_COLLECTIONS = 60 * 60 * 24 * 7  # 7 days
TTL_TAGS = 60 * 10  # 10 minutes
TTL_CATEGORIES = 60 * 10  # 10 minutes
TTL_ACTORS = 60 * 15  # 15 minutes
TTL_WORKS = 60 * 5  # 5 minutes — lean cards; client overlays want_list

KEY_PREFIX = "shadow_mdc:api:"


@dataclass(frozen=True)
class CacheStats:
    backend: str
    hits: int = 0
    misses: int = 0


class _MemoryBackend:
    def __init__(self, *, max_entries: int = 256, max_bytes: int = 32 * 1024 * 1024) -> None:
        self._store: OrderedDict[str, tuple[float | None, str, int]] = OrderedDict()
        self._max_entries = max_entries
        self._max_bytes = max_bytes
        self._size_bytes = 0
        self._lock = threading.RLock()

    def _remove(self, key: str) -> None:
        entry = self._store.pop(key, None)
        if entry is not None:
            self._size_bytes -= entry[2]

    def get(self, key: str) -> str | None:
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return None
            expires_at, payload, _ = entry
            if expires_at is not None and expires_at <= time.monotonic():
                self._remove(key)
                return None
            self._store.move_to_end(key)
            return payload

    def set(self, key: str, value: str, ttl_seconds: int | None) -> None:
        expires_at = None if ttl_seconds is None or ttl_seconds <= 0 else time.monotonic() + ttl_seconds
        size_bytes = len(key.encode("utf-8")) + len(value.encode("utf-8"))
        with self._lock:
            self._remove(key)
            if size_bytes > self._max_bytes or self._max_entries <= 0:
                return
            now = time.monotonic()
            expired = [
                key for key, (expiry, _, _) in self._store.items() if expiry is not None and expiry <= now
            ]
            for expired_key in expired:
                self._remove(expired_key)
            while self._store and (
                len(self._store) >= self._max_entries or self._size_bytes + size_bytes > self._max_bytes
            ):
                self._remove(next(iter(self._store)))
            self._store[key] = (expires_at, value, size_bytes)
            self._size_bytes += size_bytes

    def delete(self, key: str) -> None:
        with self._lock:
            self._remove(key)

    def delete_prefix(self, prefix: str) -> int:
        with self._lock:
            victims = [key for key in self._store if key.startswith(prefix)]
            for key in victims:
                self._remove(key)
            return len(victims)

    def clear(self) -> None:
        with self._lock:
            self._store.clear()
            self._size_bytes = 0


class _RedisBackend:
    def __init__(self, url: str) -> None:
        self._client = redis.Redis.from_url(
            url, decode_responses=True, socket_connect_timeout=0.5, socket_timeout=0.5
        )
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
            cursor, keys = cast(
                tuple[int, list[str]], self._client.scan(cursor=cursor, match=pattern, count=200)
            )
            if keys:
                deleted += cast(int, self._client.delete(*keys))
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
            except (redis.RedisError, ValueError, OSError) as exc:
                logger.warning("Redis unavailable (%s); using in-process cache", type(exc).__name__)
                self._backend = _MemoryBackend()
                self._backend_name = "memory"
        else:
            self._backend = _MemoryBackend()
            self._backend_name = "memory"

    def _run(self, operation: Callable[[_MemoryBackend | _RedisBackend], T]) -> T:
        backend = self._backend
        try:
            return operation(backend)
        except redis.RedisError as exc:
            if not isinstance(backend, _RedisBackend):
                raise
            with self._lock:
                if self._backend is backend:
                    self._backend = _MemoryBackend()
                    self._backend_name = "memory"
                    logger.warning("Redis connection lost (%s); using in-process cache", type(exc).__name__)
                fallback = self._backend
            return operation(fallback)

    @property
    def backend(self) -> str:
        return self._backend_name

    def stats(self) -> CacheStats:
        with self._lock:
            return CacheStats(backend=self._backend_name, hits=self._hits, misses=self._misses)

    def get_json(self, key: str) -> Any | None:
        raw = self.get_raw(key)
        if raw is None:
            return None
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            self.invalidate(key)
            return None

    def get_raw(self, key: str) -> str | None:
        """Return the cached JSON string without decoding (fast Response path)."""

        full = self._full_key(key)
        raw = self._run(lambda backend: backend.get(full))
        if raw is None:
            with self._lock:
                self._misses += 1
            return None
        with self._lock:
            self._hits += 1
        return raw

    def set_json(self, key: str, value: Any, *, ttl_seconds: int | None = TTL_STATIC) -> None:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)
        self.set_raw(key, payload, ttl_seconds=ttl_seconds)

    def set_raw(self, key: str, value: str, *, ttl_seconds: int | None = TTL_STATIC) -> None:
        """Store a pre-serialized JSON string."""

        self._run(lambda backend: backend.set(self._full_key(key), value, ttl_seconds))

    def invalidate(self, *keys: str) -> None:
        def delete(backend: _MemoryBackend | _RedisBackend) -> None:
            for key in keys:
                backend.delete(self._full_key(key))

        self._run(delete)

    def invalidate_prefix(self, prefix: str) -> int:
        return self._run(lambda backend: backend.delete_prefix(self._full_key(prefix)))

    def clear(self) -> None:
        self._run(lambda backend: backend.clear())
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
            return cast(T, cached)
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
        parsed = urlparse(url)
        host = parsed.hostname or "?"
        port = parsed.port or 6379
        return f"{host}:{port}"
    except ValueError:
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


def categories_key(*, only_with_works: bool) -> str:
    return f"categories:list:with_works={int(only_with_works)}"


def works_tags_key(limit: int) -> str:
    return f"works:tags:limit={limit}"


def actors_list_key() -> str:
    return "actors:list:v2"


def works_list_key(
    *,
    collection_id: str | None,
    collection_kind: str | None,
    collection: str | None,
    tags: tuple[str, ...],
) -> str:
    tag_part = ",".join(sorted(tags))
    return (
        f"works:list:v2:cid={collection_id or ''}:"
        f"ck={collection_kind or ''}:"
        f"cn={collection or ''}:"
        f"tags={tag_part}"
    )

