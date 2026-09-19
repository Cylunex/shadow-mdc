"""Response cache: Redis optional, memory fallback, hit/miss."""

from __future__ import annotations

import time

from shadow_mdc.services.response_cache import (
    TTL_TAGS,
    ResponseCache,
    collections_list_key,
    works_tags_key,
)


def test_memory_fallback_hit_miss_and_ttl(monkeypatch) -> None:
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
    time.sleep(1.05)
    assert cache.get_json(short) is None


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
