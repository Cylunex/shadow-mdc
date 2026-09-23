"""GFriends Filetree index + actor image fill."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from shadow_mdc.db.models import Actor
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.gfriends import GfriendsActorImageResolver, normalize_gfriends_name
from shadow_mdc.services.gfriends_fill import fill_actor_images_from_gfriends


def _sample_filetree() -> dict:
    return {
        "Information": {"TotalNum": 2, "TotalSize": 1, "Timestamp": 1},
        "Content": {
            "z-TestStudio": {
                "三上悠亜.jpg": "三上悠亜.jpg?t=1",
                "橋本ありな.jpg": "AI-Fix-橋本ありな.jpg?t=2",
            },
            "a-Other": {
                "三上悠亜.jpg": "wrong-should-not-win.jpg?t=9",
            },
        },
    }


def test_normalize_gfriends_name_strips_spaces() -> None:
    assert normalize_gfriends_name(" 三上 悠亜 ") == normalize_gfriends_name("三上悠亜")


def test_resolver_builds_index_first_wins_and_cdn_url(tmp_path: Path) -> None:
    cache = tmp_path / "Filetree.json"
    cache.write_text(json.dumps(_sample_filetree()), encoding="utf-8")
    resolver = GfriendsActorImageResolver(
        filetree_url="https://example.test/Filetree.json",
        cdn_base_url="https://cdn.example.test/base",
        cache_path=cache,
        cache_ttl_hours=24,
    )
    try:
        stats = resolver.refresh(force=False)
        assert stats["source"] == "cache_fresh"
        assert stats["entries"] >= 2
        url = resolver.resolve(["三上悠亜"])
        assert url is not None
        assert url.startswith("https://cdn.example.test/base/Content/z-TestStudio/")
        assert "三上悠亜" in url or "%E4%B8%89%E4%B8%8A" in url
        assert "?" not in url
        # spaced / alias candidate
        assert resolver.resolve([" 三上 悠亜 "]) == url
        assert resolver.resolve(["nobody-here"]) is None
    finally:
        resolver.close()


def test_fill_downloads_and_sets_local_api_url(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "test.db"
    database = Database(f"sqlite:///{db_path}")
    database.initialize()
    images_dir = tmp_path / "actor-images"
    cache = tmp_path / "cache" / "Filetree.json"
    cache.parent.mkdir(parents=True)
    cache.write_text(json.dumps(_sample_filetree()), encoding="utf-8")

    jpeg = (
        b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
        + b"\x00" * 1300
        + b"\xff\xd9"
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith(".jpg"):
            return httpx.Response(200, content=jpeg)
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    client = httpx.Client(transport=transport)

    resolver = GfriendsActorImageResolver(
        filetree_url="https://example.test/Filetree.json",
        cdn_base_url="https://cdn.example.test",
        cache_path=cache,
        cache_ttl_hours=24,
    )
    try:
        with database.session() as session:
            repo = Repository(session)
            actor = Actor(name="三上悠亜", normalized_name="三上悠亜")
            empty = Actor(name="未知演员xyz", normalized_name="未知演员xyz")
            session.add_all([actor, empty])
            session.flush()

            stats = fill_actor_images_from_gfriends(
                repo,
                resolver,
                actor_images_dir=images_dir,
                download=True,
                dry_run=False,
                http_client=client,
            )
            assert stats.matched == 1
            assert stats.filled == 1
            assert stats.downloaded == 1
            assert stats.skipped_no_match == 1
            session.refresh(actor)
            assert actor.image_url is not None
            assert actor.image_url.startswith("/api/actor-images/gfriends-")
            local = images_dir / Path(actor.image_url).name
            assert local.is_file()
            assert local.stat().st_size >= 1200
    finally:
        resolver.close()
        client.close()
