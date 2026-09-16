"""Unit tests for weekly VR seed scoring/parsers and FANZA VR helpers (offline)."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from shadow_mdc.providers.fanza import (
    FanzaRankingItem,
    _ranking_from_content,
    is_fanza_vr_item,
)
from shadow_mdc.services.weekly_vr_seed import (
    SOURCE_WEIGHTS,
    _RawHit,
    collect_vr_hits,
    parse_deovr_trending_html,
    parse_slr_popular_html,
    score_hits,
    select_seed_targets,
    sukebei_vr_hits,
    weekly_vr_tags,
)

FIXTURES = Path(__file__).parent / "fixtures" / "weekly_vr"


def test_is_fanza_vr_item_title_and_cid() -> None:
    vr = FanzaRankingItem(
        content_id="savr01135",
        rank=1,
        title="【VR】交姦キメセクNTR",
        code="SAVR-1135",
        source_url="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=savr01135/",
    )
    plain = FanzaRankingItem(
        content_id="ssis00901",
        rank=2,
        title="Non VR Title",
        code="SSIS-901",
        source_url="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=ssis00901/",
    )
    assert is_fanza_vr_item(vr) is True
    assert is_fanza_vr_item(plain) is False
    cid_only = FanzaRankingItem(
        content_id="13dsvr01760",
        rank=3,
        title="Some title without marker",
        code="DSVR-1760",
        source_url="https://example.test/",
    )
    assert is_fanza_vr_item(cid_only) is True


def test_filter_mixed_fanza_ranking_fixture() -> None:
    payload = json.loads((FIXTURES / "fanza_ranking_mixed.json").read_text(encoding="utf-8"))
    rows = payload["data"]["ppvContentRanking"]["items"]
    items: list[FanzaRankingItem] = []
    for row in rows:
        content = dict(row["content"])
        content["id"] = content.get("id") or row["id"]
        parsed = _ranking_from_content(content, rank=int(row["rank"]), base_url="https://www.dmm.co.jp")
        assert parsed is not None
        items.append(parsed)
    vr_only = [item for item in items if is_fanza_vr_item(item)]
    assert [item.code for item in vr_only] == ["SAVR-1135", "SIVR-483", "MDVR-390"]


def test_weekly_vr_tags() -> None:
    assert weekly_vr_tags(date(2026, 9, 16), 3) == (
        "weekly-vr",
        "vr",
        "weekly-vr-2026-09-16",
        "weekly-vr-rank-3",
    )


def test_score_and_select_prefer_fanza_weekly() -> None:
    hits = (
        _RawHit(
            provider="fanza",
            external_id="savr01135",
            source_url="https://example.test/savr",
            code="SAVR-1135",
            title="【VR】A",
            source="fanza_vr_weekly",
            weight=SOURCE_WEIGHTS["fanza_vr_weekly"],
            rank_hint=1,
        ),
        _RawHit(
            provider="slr",
            external_id="scene-1",
            source_url="https://example.test/slr",
            code=None,
            title="Western VR Scene",
            source="slr_popular",
            weight=SOURCE_WEIGHTS["slr_popular"],
            rank_hint=1,
        ),
        _RawHit(
            provider="fanza",
            external_id="sivr00483",
            source_url="https://example.test/sivr",
            code="SIVR-483",
            title="【VR】B",
            source="fanza_vr_daily",
            weight=SOURCE_WEIGHTS["fanza_vr_daily"],
            rank_hint=2,
        ),
        _RawHit(
            provider="javdb",
            external_id="EXIST-001",
            source_url="https://example.test/e",
            code="EXIST-001",
            title="Already there",
            source="sukebei_vr",
            weight=SOURCE_WEIGHTS["sukebei_vr"],
            rank_hint=1,
        ),
    )
    ranked = score_hits(hits)
    assert ranked[0].code == "SAVR-1135"
    selected, skipped = select_seed_targets(
        [
            c.model_copy(update={"state": "in_library"}) if c.code == "EXIST-001" else c
            for c in ranked
        ],
        limit=2,
    )
    assert [c.code or c.key for c in selected] == ["SAVR-1135", "slr:scene-1"]
    assert any(s.reason == "already_in_library" for s in skipped)


def test_parse_slr_and_deovr_fixtures() -> None:
    slr = parse_slr_popular_html((FIXTURES / "slr_popular.html").read_text(encoding="utf-8"))
    assert len(slr) >= 3
    assert all(h.provider == "slr" for h in slr)
    assert all("/scenes/" in h.source_url for h in slr)

    deovr = parse_deovr_trending_html(
        (FIXTURES / "deovr_trending.html").read_text(encoding="utf-8")
    )
    assert len(deovr) >= 3
    assert all(h.provider == "deovr" for h in deovr)
    assert deovr[0].title


def test_sukebei_vr_fixture() -> None:
    hits = sukebei_vr_hits((FIXTURES / "sukebei_vr.html").read_text(encoding="utf-8"))
    codes = [h.code for h in hits]
    assert "SIVR-444" in codes
    assert "IPVR-371" in codes
    assert "MDVR-438" in codes


@pytest.mark.asyncio
async def test_collect_vr_hits_degrades_gracefully() -> None:
    fixtures = {
        "sexlikereal.com": (FIXTURES / "slr_popular.html").read_text(encoding="utf-8"),
        "deovr.com": (FIXTURES / "deovr_trending.html").read_text(encoding="utf-8"),
        "sukebei.nyaa.si": (FIXTURES / "sukebei_vr.html").read_text(encoding="utf-8"),
    }

    async def fetch(url: str) -> str:
        for key, body in fixtures.items():
            if key in url:
                return body
        raise RuntimeError(f"unexpected url {url}")

    fanza = AsyncMock()
    fanza.fetch_ranking = AsyncMock(side_effect=RuntimeError("blocked"))

    hits, statuses = await collect_vr_hits(fanza=fanza, fetch=fetch)
    by_source = {s.source: s for s in statuses}
    assert by_source["fanza_vr_weekly"].ok is False
    assert by_source["fanza_vr_daily"].ok is False
    assert by_source["slr_popular"].ok is True
    assert by_source["deovr_trending"].ok is True
    assert by_source["sukebei_vr"].ok is True
    assert hits
    assert any(h.provider == "slr" for h in hits)
    assert any(h.provider == "deovr" for h in hits)
