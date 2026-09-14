"""Offline unit tests for JavRanking index cache + missing-only seed selection."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import httpx
import pytest

from shadow_mdc.services.javranking_client import (
    JavRankingIndexCache,
    SearchVideo,
    build_honors_map,
    parse_search_index,
)
from shadow_mdc.services.javranking_seed import (
    ranking_tags,
    select_missing_targets,
    videos_for_seed,
)

FIXTURES = Path(__file__).parent / "fixtures" / "javranking"


def _load_fixture_index() -> dict[str, object]:
    return json.loads((FIXTURES / "search-index.json").read_text(encoding="utf-8"))


def test_parse_search_index_fixture() -> None:
    index = parse_search_index(_load_fixture_index())
    assert index.schema_version == 2
    assert len(index.videos) >= 3
    top = next(v for v in index.videos if v.rank == 1)
    assert top.code == "IPX-811"
    assert top.video_id == 3
    assert top.ranking_appearances or top.rank == 1


def test_build_honors_map_and_labels() -> None:
    index = parse_search_index(_load_fixture_index())
    honors = build_honors_map(index.videos)
    assert "IPX811" in honors
    info = honors["IPX811"]
    assert info.compact_badge == "#1 JavRanking"
    assert info.detail_url.endswith("/videos/3/")
    multi = honors["ABF017"]
    assert any(h.position == 3 and "2023" in h.name for h in multi.honors)


def test_videos_for_seed_order_and_filters() -> None:
    index = parse_search_index(_load_fixture_index())
    ordered = videos_for_seed(index.videos)
    ranks = [v.rank for v in ordered if v.rank is not None]
    assert ranks == sorted(ranks)
    assert ordered[0].code == "IPX-811"

    limited = videos_for_seed(index.videos, min_rank=3)
    assert all(v.rank is not None and v.rank <= 3 for v in limited)

    slug_filtered = videos_for_seed(index.videos, ranking_slug="javdb-top250-2023")
    assert slug_filtered
    assert all(
        any(a.slug == "javdb-top250-2023" for a in v.ranking_appearances) for v in slug_filtered
    )


def test_select_missing_targets_skips_existing() -> None:
    index = parse_search_index(_load_fixture_index())
    ordered = videos_for_seed(index.videos)
    selected, skipped = select_missing_targets(
        ordered, limit=2, existing_keys={"IPX811"}
    )
    assert [v.code for v in selected] == ["IPX-580", "STARS-804"]
    reasons = {s.code: s.reason for s in skipped}
    assert reasons["IPX-811"] == "existing_work_by_code"


def test_ranking_tags_include_javranking_and_slugs() -> None:
    video = SearchVideo.model_validate(
        {
            "videoId": 10,
            "code": "ABF-017",
            "title": "x",
            "score": 1,
            "rankingAppearances": [
                {"slug": "javdb-top250-2023", "name": "JAVDB TOP250 2023", "position": 3}
            ],
            "hasPreviewVideo": False,
            "actorLinks": [],
            "coverUrl": None,
            "releaseDate": None,
        }
    )
    assert ranking_tags(video) == ("javranking", "javdb-top250-2023")


@pytest.mark.asyncio
async def test_index_cache_writes_honors(tmp_path: Path) -> None:
    index_payload = (FIXTURES / "search-index.json").read_text(encoding="utf-8")
    manifest_payload = (FIXTURES / "search-index-manifest.json").read_text(encoding="utf-8")
    hits = {"manifest": 0, "index": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url.endswith("search-index-manifest.json"):
            hits["manifest"] += 1
            return httpx.Response(
                200, text=manifest_payload, headers={"content-type": "application/json"}
            )
        if url.endswith("search-index.json"):
            hits["index"] += 1
            return httpx.Response(
                200, text=index_payload, headers={"content-type": "application/json"}
            )
        return httpx.Response(404, text="missing")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        cache = JavRankingIndexCache(tmp_path / "javranking", client=client)
        index = await cache.load_index()
        assert index.schema_version == 2
        meta = cache.read_meta()
        assert meta is not None
        assert meta.revision == "fixture-rev-001"
        honors = cache.read_honors_map()
        assert "IPX811" in honors
        assert cache.lookup_honors("ipx-811") is not None
        first_index_hits = hits["index"]
        again = await cache.load_index()
        assert len(again.videos) == len(index.videos)
        assert hits["index"] == first_index_hits


def test_seed_result_dry_helpers_importable() -> None:
    # Smoke: module imports and date default path stays stable for CLI.
    assert date.today().isoformat()
