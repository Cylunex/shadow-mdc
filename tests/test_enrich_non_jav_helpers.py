from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))

from enrich_non_jav_incomplete import (  # noqa: E402
    build_search_queries,
    extract_creator_handle,
    extract_scene_codes,
    looks_like_placeholder_work,
    plot_is_meaningful,
    resolve_tpdb_sites,
)


def test_extract_scene_codes_from_brackets_and_inline() -> None:
    assert extract_scene_codes(None, "[Ws-0005] Delivery") == ["WS-0005"]
    assert "MD-0265" in extract_scene_codes("MD-0265", "Saleswoman Md-0265")


def test_resolve_tpdb_sites_model_media_and_swaglive() -> None:
    work = SimpleNamespace(studio="Model Media US", title="The Au Pair", primary_code=None, actors=[])
    assert "modelmediaus" in resolve_tpdb_sites(work)  # type: ignore[arg-type]
    work2 = SimpleNamespace(
        studio="SwagLive: Sunnyday9",
        title="Sing",
        primary_code=None,
        actors=[],
    )
    assert "swaglivesunnyday9" in resolve_tpdb_sites(work2)  # type: ignore[arg-type]


def test_plot_is_meaningful_rejects_title_echo() -> None:
    work = SimpleNamespace(title="Hello World Scene", plot="hello world scene", original_plot=None)
    assert plot_is_meaningful(work) is False  # type: ignore[arg-type]
    work2 = SimpleNamespace(
        title="Hello",
        plot="A longer description that clearly is not just the title echo content.",
        original_plot=None,
    )
    assert plot_is_meaningful(work2) is True  # type: ignore[arg-type]


def test_build_search_queries_prefers_bracket_codes() -> None:
    work = SimpleNamespace(
        primary_code="60a21f3b3aae3be49711abc9",
        title="[Ws-0006] What Should I Do | Swag @Ezrabebe Video",
        actors=["Xinxin"],
        studio="SWAG",
        family="chinese",
    )
    queries = build_search_queries(work)  # type: ignore[arg-type]
    assert queries[0] == "WS-0006"
    assert not any(q.startswith("60a21f") for q in queries)


def test_placeholder_brand_seed_skipped() -> None:
    work = SimpleNamespace(
        title="苏畅 麻豆传媒精选",
        primary_code="MD-SUCHANG-01",
        actors=["苏畅"],
    )
    assert looks_like_placeholder_work(work) is True  # type: ignore[arg-type]


def test_resolve_tpdb_sites_from_madou_code_prefix() -> None:
    """Empty-studio titles with MD-/MSD- codes should still get site=modelmediaasia."""
    work = SimpleNamespace(
        studio=None,
        title="Saleswoman Footjob Md-0265",
        primary_code=None,
        actors=[],
    )
    assert "modelmediaasia" in resolve_tpdb_sites(work)  # type: ignore[arg-type]
    work2 = SimpleNamespace(
        studio="",
        title="Stepbrother Seduce Me",
        primary_code="MSD-001",
        actors=[],
    )
    assert "modelmediaasia" in resolve_tpdb_sites(work2)  # type: ignore[arg-type]

def test_resolve_tpdb_sites_hongkongdoll_and_peach() -> None:
    work = SimpleNamespace(studio="HongKongDoll", title="Scene", primary_code=None, actors=[])
    sites = resolve_tpdb_sites(work)  # type: ignore[arg-type]
    assert "fansdbhongkongdollonlyfans" in sites
    assert "hobbypornhongkongdoll" in sites
    work2 = SimpleNamespace(studio="蜜桃影像", title="Clip", primary_code=None, actors=[])
    assert "swaglivepeachmedia" in resolve_tpdb_sites(work2)  # type: ignore[arg-type]


def test_extract_creator_handle_and_known_map() -> None:
    assert extract_creator_handle("Jxzeroc_clip01") == "jxzeroc"
    assert extract_creator_handle("Sunnyday9_01") == "sunnyday9"
    work = SimpleNamespace(studio="", title="Sunnyday9_home", primary_code=None, actors=[])
    assert "swaglivesunnyday9" in resolve_tpdb_sites(work)  # type: ignore[arg-type]
    # Unknown handles must not invent fansdb/swaglive site guesses.
    work2 = SimpleNamespace(studio="", title="Jxzeroc_clip01", primary_code=None, actors=[])
    assert resolve_tpdb_sites(work2) == []  # type: ignore[arg-type]


def test_madou_code_prefix_routes_to_modelmediaasia() -> None:
    work = SimpleNamespace(studio="", title="Sales", primary_code="MD-0265", actors=[])
    assert "modelmediaasia" in resolve_tpdb_sites(work)  # type: ignore[arg-type]

