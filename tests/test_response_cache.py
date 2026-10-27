"""Response cache: Redis optional, memory fallback, hit/miss."""

from __future__ import annotations

import time
from typing import Never

import pytest
import redis

from shadow_mdc.services.response_cache import (
    TTL_TAGS,
    ResponseCache,
    _MemoryBackend,
    _RedisBackend,
    collections_list_key,
    works_tags_key,
)


def test_memory_fallback_hit_miss_and_ttl(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    monkeypatch.delenv("SHADOW_MDC_REDIS_URL", raising=False)
    cache = ResponseCache(redis_url="")
    assert cache.backend == "memory"

    key = collections_list_key(kind="series", q=None)
    assert cache.get_json(key) is None
    stats = cache.stats()
    assert stats.misses == 1

    payload = [{"id": "c1", "name": "Demo"}]
    cache.set_json(key, payload, ttl_seconds=TTL_TAGS)
    assert cache.get_json(key) == payload
    assert cache.stats().hits == 1

    short = works_tags_key(10)
    cache.set_json(short, {"tags": []}, ttl_seconds=1)
    assert cache.get_json(short) == {"tags": []}
    clock[0] += 1.05
    assert cache.get_json(short) is None


def test_memory_evicts_least_recently_read_entry() -> None:
    backend = _MemoryBackend(max_entries=2)
    backend.set("first", "1", None)
    backend.set("second", "2", None)
    assert backend.get("first") == "1"
    backend.set("third", "3", None)
    assert backend.get("second") is None
    assert backend.get("first") == "1"
    assert backend.get("third") == "3"


def test_memory_prunes_expired_entries_before_live_entries(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    backend = _MemoryBackend(max_entries=2)
    backend.set("live", "1", None)
    backend.set("expired", "2", 1)
    clock[0] += 2
    backend.set("new", "3", None)
    assert backend.get("live") == "1"
    assert backend.get("expired") is None
    assert backend.get("new") == "3"


def test_memory_byte_budget_counts_utf8_and_replacements() -> None:
    backend = _MemoryBackend(max_bytes=10)
    backend.set("a", "中文", None)  # 7 UTF-8 bytes, including the key
    backend.set("b", "123", None)
    assert backend.get("a") is None
    assert backend.get("b") == "123"
    backend.set("b", "x", None)
    backend.set("a", "中文", None)
    assert backend.get("b") == "x"
    assert backend.get("a") == "中文"
    backend.set("a", "too large for cache", None)
    assert backend.get("a") is None  # never serve the previous value
    assert backend.get("b") == "x"


def test_memory_releases_budget_on_delete_expire_and_clear(monkeypatch: pytest.MonkeyPatch) -> None:
    clock = [100.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    backend = _MemoryBackend(max_bytes=10)
    backend.set("a", "1234", 1)
    backend.set("b", "1234", None)
    clock[0] += 2
    assert backend.get("a") is None
    backend.set("c", "1234", None)
    assert backend.get("b") == "1234"
    backend.delete("b")
    backend.set("d", "1234", None)
    assert backend.get("c") == "1234"
    assert backend.delete_prefix("c") == 1
    backend.set("e", "1234", None)
    assert backend.get("d") == "1234"
    backend.clear()
    backend.set("f", "1234", None)
    backend.set("g", "1234", None)
    assert backend.get("f") == backend.get("g") == "1234"


@pytest.mark.parametrize("operation", ["get", "set", "delete", "delete_prefix", "clear"])
def test_redis_runtime_failure_falls_back_for_all_operations(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture, operation: str
) -> None:
    def initialize(self: _RedisBackend, url: str) -> None:
        pass

    def fail(self: _RedisBackend, *args: object) -> Never:
        raise redis.ConnectionError("private connection details")

    monkeypatch.setattr(_RedisBackend, "__init__", initialize)
    monkeypatch.setattr(_RedisBackend, operation, fail)
    cache = ResponseCache("redis://example.com/0")
    assert cache.backend == "redis"
    if operation == "get":
        assert cache.get_json("key") is None
    elif operation == "set":
        cache.set_json("key", {"ok": True})
        assert cache.get_json("key") == {"ok": True}
    elif operation == "delete":
        cache.invalidate("key", "other")
    elif operation == "delete_prefix":
        assert cache.invalidate_prefix("key") == 0
    else:
        cache.clear()
    assert cache.backend == cache.stats().backend == "memory"
    cache.set_json("after", [1, 2])
    assert cache.get_json("after") == [1, 2]
    cache.invalidate("after")
    assert cache.get_json("after") is None
    assert "private connection details" not in caplog.text


def test_invalidate_prefix() -> None:
    cache = ResponseCache(redis_url="")
    cache.set_json("collections:list:kind=:q=", [1], ttl_seconds=60)
    cache.set_json("collections:detail:abc", {"id": "abc"}, ttl_seconds=60)
    cache.set_json("javranking:sections", {"sections": []}, ttl_seconds=60)
    deleted = cache.invalidate_prefix("collections:")
    assert deleted >= 2
    assert cache.get_json("collections:list:kind=:q=") is None
    assert cache.get_json("javranking:sections") == {"sections": []}


def test_redis_unreachable_falls_back_to_memory() -> None:
    cache = ResponseCache(redis_url="redis://127.0.0.1:1/0")
    assert cache.backend == "memory"
    cache.set_json("k", {"ok": True}, ttl_seconds=30)
    assert cache.get_json("k") == {"ok": True}


def test_redis_when_available() -> None:
    cache = ResponseCache(redis_url="redis://127.0.0.1:6379/15")
    if cache.backend != "redis":
        return  # box Redis down — skip soft
    key = "test:pytest:response_cache"
    cache.invalidate(key)
    assert cache.get_json(key) is None
    cache.set_json(key, {"hello": "world"}, ttl_seconds=30)
    assert cache.get_json(key) == {"hello": "world"}
    cache.invalidate(key)
