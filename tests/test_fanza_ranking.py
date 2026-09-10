"""Unit tests for FANZA cid/code helpers and ranking parse (no network)."""

from __future__ import annotations

from shadow_mdc.providers.fanza import content_id_to_code, _ranking_from_content


def test_content_id_to_code_strips_padding() -> None:
    assert content_id_to_code("mida00726") == "MIDA-726"
    assert content_id_to_code("ssis00901") == "SSIS-901"
    assert content_id_to_code("ofes00056") == "OFES-56"


def test_ranking_from_content() -> None:
    item = _ranking_from_content(
        {
            "id": "sone00711",
            "title": "Example Title",
            "packageImage": {"largeUrl": "https://example.test/a.jpg"},
            "actresses": [{"id": "1", "name": "Actor A"}, {"id": "2", "name": "Actor B"}],
        },
        rank=3,
        base_url="https://www.dmm.co.jp",
    )
    assert item is not None
    assert item.code == "SONE-711"
    assert item.rank == 3
    assert item.actresses == ("Actor A", "Actor B")
    assert "cid=sone00711" in item.source_url
