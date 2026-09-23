"""Offline tests for JavRanking curated lists, base-url fallback, and actor honors."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest

from shadow_mdc.services.javranking_client import (
    FALLBACK_BASE_URL,
    JavRankingIndexCache,
    build_actor_honors_map,
    enrich_curated_videos_with_covers,
    parse_curated_actors_markdown,
    parse_curated_videos_markdown,
    parse_item_list_json_ld,
    parse_search_index,
    top250_year_slugs,
)
from shadow_mdc.services.javranking_seed import curated_videos_as_search, videos_for_seed

FIXTURES = Path(__file__).parent / "fixtures" / "javranking"


def test_parse_curated_videos_markdown() -> None:
    text = (FIXTURES / "most-awarded-videos.md").read_text(encoding="utf-8")
    canonical, entries = parse_curated_videos_markdown(text)
    assert canonical and canonical.endswith("/most-awarded-videos/")
    assert len(entries) == 5
    assert entries[0].code == "IPX-811"
    assert entries[0].position == 1
    assert entries[0].video_id == 3


def test_parse_curated_actors_markdown_with_scores() -> None:
    text = (FIXTURES / "most-awarded-actors.md").read_text(encoding="utf-8")
    canonical, entries = parse_curated_actors_markdown(text)
    assert canonical and "most-awarded-actors" in canonical
    assert len(entries) == 3
    assert entries[0].name == "涼森れむ"
    assert entries[0].score == 17380
    assert len(entries[0].appearances) == 2
    assert entries[0].appearances[0].code == "ABP-984"


def test_parse_html_json_ld_videos_and_actors() -> None:
    html_v = (FIXTURES / "most-awarded-videos.html").read_text(encoding="utf-8")
    _, videos, actors = parse_item_list_json_ld(html_v, kind="videos")
    assert not actors
    assert [item.code for item in videos] == ["IPX-811", "IPX-580", "STARS-804"]

    html_a = (FIXTURES / "most-awarded-actors.html").read_text(encoding="utf-8")
    _, videos2, actors2 = parse_item_list_json_ld(html_a, kind="actors")
    assert not videos2
    assert actors2[0].name == "涼森れむ"
    assert actors2[0].slug == "actor-KxPb"


def test_build_actor_honors_and_top250_years() -> None:
    female_md = (FIXTURES / "most-awarded-actors.md").read_text(encoding="utf-8")
    male_md = (FIXTURES / "most-awarded-male-actors.md").read_text(encoding="utf-8")
    _, female = parse_curated_actors_markdown(female_md)
    _, male = parse_curated_actors_markdown(male_md)
    from shadow_mdc.services.javranking_client import CuratedList

    lists = [
        CuratedList(
            slug="most-awarded-actors",
            title="女优战力",
            kind="actors",
            locale="zh-hans",
            base_url="https://javranking.cc",
            revision="a",
            fetched_at=1.0,
            source_format="markdown",
            actors=tuple(female),
        ),
        CuratedList(
            slug="most-awarded-male-actors",
            title="男优战力",
            kind="actors",
            locale="zh-hans",
            base_url="https://javranking.cc",
            revision="b",
            fetched_at=1.0,
            source_format="markdown",
            actors=tuple(male),
        ),
    ]
    honors = build_actor_honors_map(lists)
    assert "涼森れむ".casefold() in honors or any("涼森" in k for k in honors)
    rem = next(v for k, v in honors.items() if "涼森" in v.name)
    assert rem.compact_badge and "#1" in rem.compact_badge
    assert rem.honors[0].appearances == 2

    index = parse_search_index((FIXTURES / "search-index.json").read_text(encoding="utf-8"))
    years = top250_year_slugs(index.videos)
    assert any(slug.startswith("javdb-top250") for slug, _, _ in years)


def test_curated_videos_as_search_prefers_index_metadata() -> None:
    index = parse_search_index((FIXTURES / "search-index.json").read_text(encoding="utf-8"))
    text = (FIXTURES / "most-awarded-videos.md").read_text(encoding="utf-8")
    _, entries = parse_curated_videos_markdown(text)
    from shadow_mdc.services.javranking_client import CuratedList

    curated = CuratedList(
        slug="most-awarded-videos",
        title="神作 TOP100",
        kind="videos",
        locale="zh-hans",
        base_url="https://javranking.cc",
        revision="c",
        fetched_at=1.0,
        source_format="markdown",
        videos=tuple(entries),
    )
    mapped = curated_videos_as_search(curated, index_videos=index.videos)
    assert mapped[0].code == "IPX-811"
    assert mapped[0].rank == 1
    assert mapped[0].score > 0
    # TOP250 path still works
    assert videos_for_seed(index.videos, min_rank=3)


@pytest.mark.asyncio
async def test_base_url_fallback_and_curated_cache(tmp_path: Path) -> None:
    md = (FIXTURES / "most-awarded-videos.md").read_text(encoding="utf-8")
    index_payload = (FIXTURES / "search-index.json").read_text(encoding="utf-8")
    manifest_payload = (FIXTURES / "search-index-manifest.json").read_text(encoding="utf-8")
    hits = {"cc": 0, "top": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        path = request.url.path
        if host.endswith("javranking.cc"):
            hits["cc"] += 1
            raise httpx.ConnectError("dns failed", request=request)
        hits["top"] += 1
        if path.endswith("search-index-manifest.json"):
            return httpx.Response(
                200, text=manifest_payload, headers={"content-type": "application/json"}
            )
        if path.endswith("search-index.json"):
            return httpx.Response(
                200, text=index_payload, headers={"content-type": "application/json"}
            )
        if path.endswith("most-awarded-videos.md"):
            return httpx.Response(200, text=md, headers={"content-type": "text/markdown"})
        return httpx.Response(404, text="missing")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cache = JavRankingIndexCache(tmp_path / "javranking", client=client)
        index = await cache.load_index()
        assert index.schema_version == 2
        assert cache.base_url == FALLBACK_BASE_URL.rstrip("/")
        curated = await cache.load_curated_list("most-awarded-videos")
        assert curated.slug == "most-awarded-videos"
        assert len(curated.videos) == 5
        assert cache.read_curated_list("most-awarded-videos") is not None
        assert hits["cc"] >= 1
        assert hits["top"] >= 1


@pytest.mark.asyncio
async def test_actor_honors_cache_written(tmp_path: Path) -> None:
    female = (FIXTURES / "most-awarded-actors.md").read_text(encoding="utf-8")
    male = (FIXTURES / "most-awarded-male-actors.md").read_text(encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("most-awarded-actors.md"):
            return httpx.Response(200, text=female, headers={"content-type": "text/markdown"})
        if path.endswith("most-awarded-male-actors.md"):
            return httpx.Response(200, text=male, headers={"content-type": "text/markdown"})
        return httpx.Response(404, text="missing")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cache = JavRankingIndexCache(tmp_path / "javranking", client=client)
        await cache.load_curated_list("most-awarded-actors")
        await cache.load_curated_list("most-awarded-male-actors")
        honors = cache.read_actor_honors_map()
        assert honors
        hit = cache.lookup_actor_honors("涼森れむ")
        assert hit is not None
        assert hit.compact_badge


def test_enrich_curated_videos_with_covers_from_search_index() -> None:
    index = parse_search_index((FIXTURES / "search-index.json").read_text(encoding="utf-8"))
    text = (FIXTURES / "most-awarded-videos.md").read_text(encoding="utf-8")
    _, entries = parse_curated_videos_markdown(text)
    assert all(entry.cover_url is None for entry in entries)
    enriched = enrich_curated_videos_with_covers(entries, index.videos)
    assert enriched[0].code == "IPX-811"
    assert enriched[0].cover_url and enriched[0].cover_url.startswith("http")
    # Idempotent when covers already present
    again = enrich_curated_videos_with_covers(enriched, index.videos)
    assert again[0].cover_url == enriched[0].cover_url
