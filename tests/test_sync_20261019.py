"""Sync 2026-10-19 P0: remaster marker identity, marker-aware CIDs, actor wipe fence."""

from __future__ import annotations

from pathlib import Path

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.dmm_ids import content_id_candidates, content_id_candidates_with_marker
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.identity import clean_stem, extract_code
from shadow_mdc.remaster import parse_remaster_identity, remaster_display, strip_remaster_marker


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    return database


@pytest.mark.parametrize(
    ("raw", "base"),
    [
        ("RCT-156HD.mp4", "RCT-156"),
        ("RCT-156-HD.mp4", "RCT-156"),
        ("RCT-156H.mp4", "RCT-156"),
        ("RCT-156AI.mp4", "RCT-156"),
        ("SSIS-123-HD.mp4", "SSIS-123"),
        ("T28-557H.mp4", "T28-557"),
        ("IPX-535Z-HD.mp4", "IPX-535Z"),
        ("4K-SSIS-123.mp4", "SSIS-123"),
        ("SSIS-123.mp4", "SSIS-123"),
    ],
)
def test_extract_code_strips_remaster_marker_for_lookup(raw: str, base: str) -> None:
    code, family = extract_code(clean_stem(raw))
    assert family is ContentFamily.JAV
    assert code == base


def test_leading_quality_token_is_not_the_jav_id() -> None:
    code, family = extract_code(clean_stem("4K-ABC-123.mp4"))
    assert family is ContentFamily.JAV
    assert code == "ABC-123"
    assert code is not None and not code.startswith("4K")


def test_remaster_parse_and_display() -> None:
    parsed = parse_remaster_identity("RCT-156HD")
    assert parsed is not None
    assert parsed.base == "RCT-156"
    assert parsed.display == "RCT-156H"
    assert parsed.content_marker == "h"
    assert strip_remaster_marker("DV-818-AI") == "DV-818"
    assert remaster_display("DV-818AI") == "DV-818AI"
    assert parse_remaster_identity("SSIS-123") is None


def test_content_id_candidates_reappend_folded_marker() -> None:
    for value in ("RCT-156H", "RCT-156HD", "RCT-156-HD"):
        cands = content_id_candidates(value)
        assert cands
        assert all(c.endswith("h") for c in cands), cands
        assert not any(c.endswith("hd") for c in cands), cands
        assert "rct156h" in cands or "1rct00156h" in cands

    ai = content_id_candidates("DV-818AI")
    assert ai and all("ai" in c for c in ai)

    assert content_id_candidates("RCT-156") == content_id_candidates_with_marker("RCT-156")


def test_content_id_candidates_pin_t28_not_series_t() -> None:
    cands = content_id_candidates("T28-1H")
    assert "9t2800001h" in cands or "9t28001h" in cands
    for item in cands:
        assert "t00281" not in item, item
        assert item.endswith("h")

    base = content_id_candidates("T28-557")
    assert any(item.startswith("9t28") or item.startswith("t28") for item in base)
    assert not any(item.startswith("t00028") for item in base[:6])


def test_channel_prefixed_remaster_cid_leads() -> None:
    cands = content_id_candidates("1RCT00156H")
    assert cands[0] == "1rct00156h"


def test_force_overwrite_preserves_named_actors_when_incoming_empty(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        repo = Repository(session)
        seed = ProviderRecord(
            provider="javdb",
            external_id="seed-1",
            title="Seed",
            family=ContentFamily.JAV,
            category=MediaCategory.JAPAN,
            code="SSIS-001",
            actors=("三上悠亜",),
        )
        work = repo.upsert_provider_record(seed, overwrite=True)
        assert work.actors == ["三上悠亜"]

        empty = ProviderRecord(
            provider="r18dev",
            external_id="empty-1",
            title="Seed refreshed",
            family=ContentFamily.JAV,
            category=MediaCategory.JAPAN,
            code="SSIS-001",
            actors=(),
        )
        work = repo.upsert_provider_record(empty, overwrite=True)
        assert work.actors == ["三上悠亜"]


def test_update_work_fields_rejects_destructive_empty_actors(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        repo = Repository(session)
        seed = ProviderRecord(
            provider="javdb",
            external_id="seed-2",
            title="Seed",
            family=ContentFamily.JAV,
            category=MediaCategory.JAPAN,
            code="SSIS-002",
            actors=("河北彩花",),
        )
        work = repo.upsert_provider_record(seed, overwrite=True)
        with pytest.raises(ValueError, match="refusing to clear named actors"):
            repo.update_work_fields(work, actors=[])
        assert work.actors == ["河北彩花"]
