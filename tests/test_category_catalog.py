"""Category cover catalog mapping, manifest, and API."""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.services.category_catalog import (
    FEATURED_CATEGORY_LABELS,
    CategoryCoverEntry,
    build_category_list,
    load_manifest,
    slugify_label,
    write_manifest,
)
from shadow_mdc.tags import canonicalize_tag


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("素人", "素人"),
        ("Amateur", "素人"),
        ("巨乳", "巨乳"),
        ("Big Tits", "巨乳"),
        ("内射中出", "中出"),
        ("Creampie", "中出"),
        ("第一视角", "POV"),
        ("POV", "POV"),
        ("辣妈", "MILF"),
        ("MILF", "MILF"),
        ("肥臀", "美尻"),
        ("虚拟现实", "VR"),
        ("VR Porn", "VR"),
        ("Students", "学生"),
        ("nurses", "护士"),
        ("Bondage", "捆绑"),
        ("Hentai", "里番"),
        ("Cosplay", "Cosplay"),
        ("BBC", "BBC"),
        ("BDSM", "BDSM"),
        ("Titty Fucking", "乳交"),
        ("Cum Swallowing", "吞精"),
        ("Doggy Style", "后入"),
        ("Cowgirl", "骑乘"),
        ("Missionary", "正常位"),
        ("Cheating", "不倫"),
        ("出轨", "不倫"),
        ("Oiled", "油腻"),
        ("Lactating", "母乳"),
        ("Stepsister", "姐妹"),
        ("Housewife", "人妻"),
        ("Shaving", "刮毛"),
        ("Dirty Talk", "淫语"),
        ("Humiliation", "羞辱"),
        ("Virgin", "童贞"),
        ("Doctor", "女医"),
        ("Webcam", "摄像头"),
        ("Bathroom", "浴室"),
    ],
)
def test_category_source_titles_map_to_canonical(raw: str, expected: str) -> None:
    assert canonicalize_tag(raw) == expected


def test_featured_labels_are_curated_size() -> None:
    assert 40 <= len(FEATURED_CATEGORY_LABELS) <= 150
    assert len(FEATURED_CATEGORY_LABELS) == len(set(FEATURED_CATEGORY_LABELS))


def test_manifest_roundtrip_and_build_list(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    covers = data_dir / "category-covers"
    covers.mkdir(parents=True)
    image = covers / "素人-abcdef1234.jpg"
    image.write_bytes(b"\xff\xd8\xff" + b"x" * 64)
    write_manifest(
        data_dir,
        [
            CategoryCoverEntry(
                slug=slugify_label("素人"),
                label="素人",
                image_file=image.name,
                aliases=("Amateur",),
                source="pornhub",
                source_title="素人",
            ),
            CategoryCoverEntry(
                slug=slugify_label("巨乳"),
                label="巨乳",
                image_file=None,
                aliases=("Big Tits",),
            ),
        ],
    )
    loaded = load_manifest(data_dir)
    assert [item.label for item in loaded] == ["素人", "巨乳"]
    rows = build_category_list(
        data_dir,
        facet_counts={"素人": 12, "巨乳": 0},
        only_with_works=False,
    )
    by_label = {row.label: row for row in rows}
    assert by_label["素人"].work_count == 12
    assert by_label["素人"].image_url == f"/api/category-covers/{image.name}"
    only = build_category_list(
        data_dir,
        facet_counts={"素人": 12, "巨乳": 0},
        only_with_works=True,
    )
    assert [row.label for row in only] == ["素人"]


def test_categories_api_returns_manifest_and_cover(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'cat.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    covers = data_dir / "category-covers"
    covers.mkdir(parents=True)
    image = covers / "pov-test.jpg"
    image.write_bytes(b"\xff\xd8\xfffixture")
    write_manifest(
        data_dir,
        [
            CategoryCoverEntry(
                slug="pov",
                label="POV",
                image_file=image.name,
                aliases=("第一视角",),
            )
        ],
    )

    with TestClient(app) as client:
        payload = client.get("/api/categories").json()
        assert "categories" in payload
        labels = {item["label"] for item in payload["categories"]}
        assert "POV" in labels
        pov = next(item for item in payload["categories"] if item["label"] == "POV")
        assert pov["image_url"] == "/api/category-covers/pov-test.jpg"
        assert pov["work_count"] == 0
        image_response = client.get("/api/category-covers/pov-test.jpg")
        assert image_response.status_code == 200
        assert image_response.content.startswith(b"\xff\xd8\xff")
        missing = client.get("/api/category-covers/nope.jpg")
        assert missing.status_code == 404
