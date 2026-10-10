"""Post-seed auto-offline: pick best magnet (code-match) and enqueue via pan."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from shadow_mdc.db.models import WorkMagnet
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.pan import PanService
from shadow_mdc.services.seed_auto_offline import (
    collect_new_seed_work_ids,
    enqueue_seeded_offline,
    resolve_work_ids_for_day,
)


def test_collect_new_seed_work_ids_filters_created() -> None:
    seeded = [
        SimpleNamespace(work_id="a", created=True),
        SimpleNamespace(work_id="b", created=False),
        SimpleNamespace(work_id="dry-run", created=True),
        SimpleNamespace(work_id="a", created=True),
    ]
    assert collect_new_seed_work_ids(seeded=seeded) == ["a"]


@pytest.mark.asyncio
async def test_enqueue_seeded_offline_picks_code_match(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    db_path = tmp_path / "t.db"
    database = Database(f"sqlite:///{db_path}")
    database.initialize()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pan = PanService(data_dir=data_dir)
    pan.config_store.save(
        pan.config_store.load().model_copy(
            update={
                "pan_backend": "openlist",
                "openlist_base_url": "http://openlist.test",
                "openlist_offline_path": "/media/115/云下载",
                "subscription_auto_offline": True,
            }
        )
    )

    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="x1",
                title="Title",
                code="SSIS-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/x1",
                actors=["A"],
                release_date=date(2026, 1, 1),
                tags=["daily-chart-2026-10-10"],
            ),
            overwrite=True,
        )
        wrong = WorkMagnet(
            work_id=work.id,
            provider="javdb",
            info_hash="A" * 40,
            uri="magnet:?xt=urn:btih:" + "A" * 40,
            name="WRONG-999",
            has_subtitle=True,
            hd=True,
            size_bytes=20 << 30,
        )
        right = WorkMagnet(
            work_id=work.id,
            provider="javdb",
            info_hash="B" * 40,
            uri="magnet:?xt=urn:btih:" + "B" * 40,
            name="SSIS-001-C",
            has_subtitle=True,
            hd=True,
            size_bytes=8 << 30,
        )
        session.add_all([wrong, right])
        session.flush()
        work_id = work.id
        assert resolve_work_ids_for_day(repo, day=date(2026, 10, 10)) == [work_id]

    submitted: list[str] = []

    async def fake_submit(url: str, **kwargs: object) -> dict[str, object]:
        submitted.append(str(url))
        return {"info_hash": "B" * 40, "remote_task_id": "t1"}

    monkeypatch.setattr(pan, "status", lambda: {"connected": True, "offline_ready": True})
    monkeypatch.setattr(pan, "backend", lambda: "openlist")
    monkeypatch.setattr(pan, "offline_target", lambda: "/media/115/云下载")
    monkeypatch.setattr(pan, "submit_offline_url", AsyncMock(side_effect=fake_submit))

    discover = SimpleNamespace(sukebei_available=False, list_magnets=AsyncMock(return_value=()))

    stats = await enqueue_seeded_offline(
        database=database,
        pan=pan,
        discover=discover,  # type: ignore[arg-type]
        work_ids=[work_id],
        pace_seconds=0,
    )
    assert stats.submitted == 1
    assert submitted and submitted[0].endswith("B" * 40)


@pytest.mark.asyncio
async def test_enqueue_seeded_offline_skips_when_pan_not_ready(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = Database(f"sqlite:///{tmp_path / 't.db'}")
    database.initialize()
    pan = PanService(data_dir=tmp_path / "data")
    monkeypatch.setattr(pan, "status", lambda: {"connected": False})
    stats = await enqueue_seeded_offline(
        database=database,
        pan=pan,
        discover=SimpleNamespace(sukebei_available=False),  # type: ignore[arg-type]
        work_ids=["missing"],
        pace_seconds=0,
    )
    assert stats.skipped_pan == 1
    assert stats.submitted == 0
