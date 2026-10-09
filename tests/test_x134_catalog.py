"""Offline fixtures for 134x list/detail catalog parsers."""

from __future__ import annotations

from pathlib import Path

from shadow_mdc.services.x134_catalog import (
    codes_from_snapshot,
    handles_from_snapshot,
    merge_refs,
    parse_list_html,
    parse_video_detail_html,
    X134CatalogSnapshot,
)

FIXTURES = Path(__file__).parent / "fixtures" / "x134"


def test_parse_popular_list_extracts_video_ids() -> None:
    html = (FIXTURES / "popular.html").read_text(encoding="utf-8")
    refs = parse_list_html(html, list_name="popular")
    assert len(refs) >= 5
    assert refs[0].rank == 1
    assert refs[0].video_id.isdigit()
    assert "/video/" in refs[0].href
    ids = [r.video_id for r in refs]
    assert len(ids) == len(set(ids))


def test_parse_featured_list() -> None:
    html = (FIXTURES / "featured.html").read_text(encoding="utf-8")
    refs = parse_list_html(html, list_name="featured")
    assert len(refs) >= 3


def test_parse_video_detail_og_without_stream_url() -> None:
    html = (FIXTURES / "video_detail.html").read_text(encoding="utf-8")
    detail = parse_video_detail_html(html, video_id="2085394718331183429")
    assert detail.code == "JUR-754"
    assert detail.x_handle == "shiyu65301"
    assert detail.duration_seconds == 7027
    assert detail.poster_url and detail.poster_url.startswith("https://")
    assert "JUR-754" in detail.title
    # Must not leak amplify stream URL into any stored field we care about.
    blob = detail.model_dump_json()
    assert "video.twimg.com" not in blob
    assert "amplify_video" not in blob


def test_merge_and_codes_prefer_details() -> None:
    popular = parse_list_html(
        (FIXTURES / "popular.html").read_text(encoding="utf-8"), list_name="popular"
    )
    featured = parse_list_html(
        (FIXTURES / "featured.html").read_text(encoding="utf-8"), list_name="featured"
    )
    merged = merge_refs(popular, featured)
    assert merged
    detail = parse_video_detail_html(
        (FIXTURES / "video_detail.html").read_text(encoding="utf-8"),
        video_id="2085394718331183429",
    )
    snapshot = X134CatalogSnapshot(
        fetched_at="2026-10-09T00:00:00+00:00",
        lists=("popular", "featured"),
        refs=merged,
        details=(detail,),
    )
    codes = codes_from_snapshot(snapshot)
    assert codes[0] == "JUR-754"
    handles = handles_from_snapshot(snapshot)
    assert "shiyu65301" in handles


def test_challenge_html_returns_empty() -> None:
    assert parse_list_html("Just a moment... Cloudflare", list_name="popular") == ()


def test_handle_like_tokens_are_not_codes() -> None:
    from shadow_mdc.services.x134_catalog import _extract_codes

    assert _extract_codes("AV精选推荐 (@shiyu65301) 發佈") == []
    assert _extract_codes("JUR-754 「就插一下」 @shiyu65301") == ["JUR-754"]
