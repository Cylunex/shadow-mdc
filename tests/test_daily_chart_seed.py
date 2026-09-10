"""Unit tests for daily chart scoring / selection (no network)."""

from __future__ import annotations

from datetime import date

import pytest

from shadow_mdc.services.daily_chart_seed import (
    LIST_WEIGHTS,
    appearance_score,
    candidate_key,
    chart_tags,
    list_rank_points,
    merge_tags,
    score_browse_pages,
    select_seed_targets,
)
from shadow_mdc.services.discover import DiscoverItem


def _item(
    *,
    code: str | None,
    title: str,
    external_id: str,
    state: str = "not_in_library",
) -> DiscoverItem:
    return DiscoverItem(
        provider="javdb",
        external_id=external_id,
        source_url=f"https://javdb.com/v/{external_id}",
        code=code,
        title=title,
        state=state,  # type: ignore[arg-type]
    )


def test_list_rank_points_prefer_better_rank() -> None:
    assert list_rank_points(1) > list_rank_points(2)
    assert list_rank_points(1) == 50.0
    assert list_rank_points(50) == 1.0
    assert list_rank_points(51) == 1.0
    with pytest.raises(ValueError):
        list_rank_points(0)


def test_appearance_score_uses_list_weights() -> None:
    daily = appearance_score("rankings_daily", 1)
    weekly = appearance_score("rankings_weekly", 1)
    monthly = appearance_score("rankings_monthly", 1)
    latest = appearance_score("latest", 1)
    assert daily == LIST_WEIGHTS["rankings_daily"] * 50.0
    assert daily > weekly > monthly > latest


def test_score_browse_pages_consensus_and_merge() -> None:
    pages = {
        "rankings_daily": (
            _item(code="AAA-001", title="Daily One", external_id="d1"),
            _item(code="BBB-002", title="Daily Two", external_id="d2"),
        ),
        "rankings_weekly": (
            _item(code="AAA-001", title="Weekly One Longer Title", external_id="d1"),
            _item(code="CCC-003", title="Weekly Only", external_id="w3"),
        ),
        "rankings_monthly": (
            _item(code=None, title="No Code Yet", external_id="d1"),
        ),
        "latest": (
            _item(code="DDD-004", title="Latest Only", external_id="l4"),
        ),
    }
    ranked = score_browse_pages(pages)
    assert ranked
    top = ranked[0]
    assert top.code == "AAA-001"
    assert top.list_count >= 2
    assert top.score == appearance_score("rankings_daily", 1) + appearance_score(
        "rankings_weekly", 1
    ) + appearance_score("rankings_monthly", 1)
    assert any(a.list_name == "rankings_monthly" for a in top.appearances)
    keys = [c.key for c in ranked]
    assert keys.count("AAA-001") == 1
    assert candidate_key(pages["rankings_daily"][0]) == "AAA-001"


def test_select_seed_targets_skips_library_and_existing() -> None:
    candidates = score_browse_pages(
        {
            "rankings_daily": (
                _item(code="KEEP-001", title="Keep", external_id="k1"),
                _item(code="LIB-002", title="Lib", external_id="l2", state="in_library"),
                _item(code="CAT-003", title="Cat", external_id="c3", state="catalog_only"),
                _item(code="EXIST-004", title="Exist", external_id="e4"),
                _item(code="KEEP-005", title="Keep5", external_id="k5"),
            )
        }
    )
    selected, skipped = select_seed_targets(
        candidates, limit=2, existing_codes={"EXIST-004"}
    )
    assert [c.code for c in selected] == ["KEEP-001", "KEEP-005"]
    reasons = {s.code: s.reason for s in skipped}
    assert reasons["LIB-002"] == "already_in_library"
    assert reasons["CAT-003"] == "already_catalog_only"
    assert reasons["EXIST-004"] == "existing_work_by_code"


def test_select_seed_targets_beyond_limit() -> None:
    pages = {
        "latest": tuple(
            _item(code=f"X-{i:03d}", title=f"T{i}", external_id=f"id{i}") for i in range(1, 6)
        )
    }
    candidates = score_browse_pages(pages)
    selected, skipped = select_seed_targets(candidates, limit=2)
    assert len(selected) == 2
    assert sum(1 for s in skipped if s.reason == "beyond_limit") == 3


def test_chart_tags_and_merge_tags() -> None:
    day = date(2026, 9, 10)
    assert chart_tags(day, 3) == ("daily-chart-2026-09-10", "daily-chart-rank-3")
    assert merge_tags(["a", "b"], ["b", "c", "  "]) == ["a", "b", "c"]


def test_browse_page_key_and_fanza_weights() -> None:
    from shadow_mdc.services.daily_chart_seed import LIST_WEIGHTS, browse_page_key

    assert browse_page_key("javdb", "rankings_daily") == "rankings_daily"
    assert browse_page_key("fanza", "rankings_daily") == "fanza_rankings_daily"
    assert LIST_WEIGHTS["fanza_rankings_daily"] == LIST_WEIGHTS["rankings_daily"]


def test_score_browse_pages_merges_fanza_and_javdb() -> None:
    pages = {
        "rankings_daily": (
            _item(code="AAA-001", title="Daily", external_id="d1"),
        ),
        "fanza_rankings_daily": (
            DiscoverItem(
                provider="fanza",
                external_id="aaa00001",
                source_url="https://www.dmm.co.jp/digital/videoa/-/detail/=/cid=aaa00001/",
                code="AAA-001",
                title="Fanza Daily Longer Title",
            ),
        ),
    }
    ranked = score_browse_pages(pages)
    assert ranked[0].code == "AAA-001"
    assert ranked[0].list_count == 2
    assert ranked[0].score == appearance_score("rankings_daily", 1) + appearance_score(
        "fanza_rankings_daily", 1
    )
