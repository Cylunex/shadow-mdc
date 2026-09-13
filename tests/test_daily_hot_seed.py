"""Unit tests for daily hot/buzz scoring and parsers (no live network)."""

from __future__ import annotations

from datetime import date
from pathlib import Path

import pytest

from shadow_mdc.services.daily_hot_seed import (
    SOURCE_WEIGHTS,
    CodeMention,
    extract_codes_from_text,
    hot_tags,
    mention_points,
    parse_freejavbt_rank_html,
    parse_reddit_hot_json,
    parse_sukebei_list_html,
    parse_x_mirror_html,
    score_mentions,
    select_seed_targets,
)

FIXTURES = Path(__file__).parent / "fixtures" / "daily_hot"


def test_extract_codes_from_text_jav_and_fc2() -> None:
    text = "今日 SSIS-999 と FC2-PPV-1234567 それと SONE001 MIDV-100"
    codes = extract_codes_from_text(text)
    assert "SSIS-999" in codes
    assert "FC2-1234567" in codes
    assert "MIDV-100" in codes
    # SONE001 without separator may or may not parse; when present it normalizes.
    assert codes.count("SSIS-999") == 1


def test_mention_points_and_score_mentions_prefer_multi_source() -> None:
    assert mention_points(5, 0) == 5.0
    assert mention_points(3, 99) > mention_points(3, 0)
    mentions = (
        CodeMention(code="SSIS-999", source="x_twitter", weight=SOURCE_WEIGHTS["x_twitter"], engagement=10),
        CodeMention(code="SSIS-999", source="reddit", weight=SOURCE_WEIGHTS["reddit"], engagement=50),
        CodeMention(code="SONE-001", source="sukebei_seeders", weight=SOURCE_WEIGHTS["sukebei_seeders"], engagement=5),
        CodeMention(code="IPZZ-001", source="freejavbt_day", weight=SOURCE_WEIGHTS["freejavbt_day"], engagement=20),
    )
    ranked = score_mentions(mentions)
    assert ranked[0].code == "SSIS-999"
    assert ranked[0].mention_count == 2
    assert "reddit" in ranked[0].sources and "x_twitter" in ranked[0].sources
    assert ranked[0].score > ranked[1].score


def test_select_seed_targets_skips_library_and_existing() -> None:
    candidates = score_mentions(
        (
            CodeMention(code="KEEP-001", source="reddit", weight=4),
            CodeMention(code="LIB-002", source="reddit", weight=4),
            CodeMention(code="EXIST-003", source="reddit", weight=3),
            CodeMention(code="KEEP-004", source="reddit", weight=2),
            CodeMention(code="TAIL-005", source="reddit", weight=1),
        )
    )
    # Inject library state on LIB-002
    projected = []
    for item in candidates:
        if item.code == "LIB-002":
            projected.append(item.model_copy(update={"state": "in_library"}))
        else:
            projected.append(item)
    selected, skipped = select_seed_targets(
        projected, limit=2, existing_codes={"EXIST-003"}
    )
    assert [c.code for c in selected] == ["KEEP-001", "KEEP-004"]
    reasons = {s.code: s.reason for s in skipped}
    assert reasons["LIB-002"] == "already_in_library"
    assert reasons["EXIST-003"] == "existing_work_by_code"
    assert reasons["TAIL-005"] == "beyond_limit"


def test_hot_tags() -> None:
    assert hot_tags(date(2026, 9, 13), 2) == (
        "hot-buzz",
        "daily-hot-2026-09-13",
        "daily-hot-rank-2",
    )


def test_parse_sukebei_fixture() -> None:
    html = (FIXTURES / "sukebei_seeders.html").read_text(encoding="utf-8")
    mentions = parse_sukebei_list_html(html, source="sukebei_seeders")
    codes = [m.code for m in mentions]
    assert "SSIS-999" in codes
    assert "FC2-1234567" in codes
    assert "SONE-001" in codes
    assert "MIDV-100" in codes
    ssis = next(m for m in mentions if m.code == "SSIS-999")
    assert ssis.engagement >= 100


def test_parse_reddit_and_freejavbt_fixtures() -> None:
    reddit = parse_reddit_hot_json((FIXTURES / "reddit_hot.json").read_text(encoding="utf-8"))
    assert {m.code for m in reddit} >= {"SSIS-999", "SONE-001"}
    ssis = next(m for m in reddit if m.code == "SSIS-999")
    assert ssis.engagement >= 120

    day = parse_freejavbt_rank_html(
        (FIXTURES / "freejavbt_day.html").read_text(encoding="utf-8"),
        source="freejavbt_day",
    )
    assert [m.code for m in day][:3] == ["SNOS-401", "IPZZ-927", "SSIS-999"]


def test_parse_x_mirror_ok_and_antibot() -> None:
    tweets = parse_x_mirror_html((FIXTURES / "xcancel_tweets.html").read_text(encoding="utf-8"))
    assert {m.code for m in tweets} >= {"SSIS-999", "SONE-001", "MIDV-100"}
    with pytest.raises(RuntimeError, match="antibot"):
        parse_x_mirror_html((FIXTURES / "xcancel_antibot.html").read_text(encoding="utf-8"))


@pytest.mark.asyncio
async def test_collect_buzz_mentions_degrades_gracefully(tmp_path: Path) -> None:
    from shadow_mdc.services.daily_hot_seed import collect_buzz_mentions

    fixtures = {
        "xcancel.com": (FIXTURES / "xcancel_antibot.html").read_text(encoding="utf-8"),
        "nitter": (FIXTURES / "xcancel_antibot.html").read_text(encoding="utf-8"),
        "reddit.com": "<html>blocked</html>",
        "sukebei.nyaa.si/?f=0&c=2_2&q=&s=seeders": (FIXTURES / "sukebei_seeders.html").read_text(
            encoding="utf-8"
        ),
        "sukebei.nyaa.si/?f=0&c=2_2&q=&s=id": (FIXTURES / "sukebei_recent.html").read_text(
            encoding="utf-8"
        ),
        "freejavbt.com/rank/censored/day": (FIXTURES / "freejavbt_day.html").read_text(
            encoding="utf-8"
        ),
        "freejavbt.com/rank/censored/week": (FIXTURES / "freejavbt_day.html").read_text(
            encoding="utf-8"
        ),
        "javdb.com": "<html><body></body></html>",
        "javlibrary.com": "<html><title>Just a moment...</title></html>",
    }

    async def fetch(url: str) -> str:
        for key, body in fixtures.items():
            if key in url:
                return body
        raise RuntimeError(f"unexpected url {url}")

    mentions, statuses = await collect_buzz_mentions(fetch)
    by_source = {s.source: s for s in statuses}
    assert by_source["x_twitter"].ok is False
    assert by_source["reddit"].ok is False
    assert by_source["sukebei_seeders"].ok is True
    assert by_source["sukebei_recent"].ok is True
    assert by_source["freejavbt_day"].ok is True
    assert by_source["javlibrary_mostwanted"].ok is False
    codes = {m.code for m in mentions}
    assert "SSIS-999" in codes
    assert "PRED-888" in codes
    _ = tmp_path  # keep pytest tmp available if later extended
