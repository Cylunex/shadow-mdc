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
from shadow_mdc.services.offline_recovery import seed_recovery
from shadow_mdc.services.pan import PanService
from shadow_mdc.services.pan_offline_enqueue import enqueue_work_offline
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
async def test_enqueue_seeded_offline_picks_code_match(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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
                actors=("A",),
                release_date=date(2026, 1, 1),
                tags=("daily-chart-2026-10-10",),
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
async def test_enqueue_seeded_offline_skips_when_pan_not_ready(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
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


@pytest.mark.asyncio
async def test_auto_offline_keeps_exhausted_magnets_terminal_and_allows_new_candidates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    database = Database(f"sqlite:///{tmp_path / 't.db'}")
    database.initialize()
    pan = PanService(data_dir=tmp_path / "data")
    pan.save_settings({"offline_directory_id": "target"})
    monkeypatch.setattr(pan, "status", lambda: {"connected": True, "offline_ready": True})
    submit = AsyncMock(side_effect=lambda url, **kwargs: {"info_hash": kwargs["info_hash_hint"]})
    monkeypatch.setattr(pan, "submit_offline_url", submit)
    discover = SimpleNamespace(sukebei_available=False, list_magnets=AsyncMock(return_value=()))
    failed_hash = "A" * 40
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(ProviderRecord(
            provider="fixture", external_id="exhausted", code="ABC-123", title="Title",
            family=ContentFamily.JAV,
        ), overwrite=True)
        work_id = work.id
        repo.save_work_magnets(work, [{
            "uri": "magnet:?xt=urn:btih:" + failed_hash, "info_hash": failed_hash,
            "name": "ABC-123-C", "size_bytes": 10 << 30, "has_subtitle": True,
        }], provider="fixture")
        state = seed_recovery(info_hash=failed_hash, magnet_id=None, url=None)
        state.exhausted = True
        task = repo.create_pan_offline_task(
            work_id=work_id, info_hash=failed_hash, directory_id="target", recovery_json=state.to_dict(),
        )
        repo.update_pan_offline_task(task, status="failed", error="code: 10004, message: 错误的链接")

    for _ in range(2):
        stats = await enqueue_seeded_offline(
            database=database, pan=pan, discover=discover,  # type: ignore[arg-type]
            work_ids=[work_id], pace_seconds=0,
        )
        assert stats.submitted == 0 and stats.hunting == 1
    submit.assert_not_awaited()

    with database.session() as session:
        repo = Repository(session)
        stored_work = repo.get_work(work_id)
        assert stored_work is not None
        repo.save_work_magnets(stored_work, [{
            "uri": "magnet:?xt=urn:btih:" + "B" * 40, "info_hash": "B" * 40,
            "name": "ABC-123", "size_bytes": 1 << 30,
        }], provider="new-source")
    stats = await enqueue_seeded_offline(
        database=database, pan=pan, discover=discover,  # type: ignore[arg-type]
        work_ids=[work_id], pace_seconds=0,
    )
    assert stats.submitted == 1
    assert submit.await_args is not None and submit.await_args.kwargs["info_hash_hint"] == "B" * 40
    # An explicit user retry is still allowed to override the automatic attempt limit.
    with database.session() as session:
        repo = Repository(session)
        magnet = next(item for item in repo.list_work_magnets(work_id) if item.info_hash == failed_hash)
        result = await enqueue_work_offline(repo, pan, work_id, magnet_id=magnet.id)
        assert result.task.status == "running"
    assert submit.await_count == 2


@pytest.mark.asyncio
async def test_enqueue_seeded_offline_hunting_adds_want_list(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No magnet → hunting → auto-add work_id to want_list for SubscriptionWatch."""

    database = Database(f"sqlite:///{tmp_path / 't.db'}")
    database.initialize()
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    pan = PanService(data_dir=data_dir)
    monkeypatch.setattr(pan, "status", lambda: {"connected": True, "offline_ready": True})

    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="hunt1",
                title="Hunting Title",
                code="PRWF-17",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/hunt1",
                actors=("A",),
                release_date=date(2026, 1, 1),
            ),
            overwrite=True,
        )
        work_id = work.id

    discover = SimpleNamespace(
        sukebei_available=False,
        list_magnets=AsyncMock(return_value=()),
    )
    stats = await enqueue_seeded_offline(
        database=database,
        pan=pan,
        discover=discover,  # type: ignore[arg-type]
        work_ids=[work_id],
        pace_seconds=0,
        data_dir=data_dir,
    )
    assert stats.hunting == 1
    assert stats.want_list_added == 1
    assert stats.submitted == 0
    from shadow_mdc.services.library_prefs import LibraryPrefsStore

    prefs = LibraryPrefsStore(data_dir / "library-prefs.json").load()
    assert work_id in prefs.want_list

    # Idempotent: second pass of same hunting work does not double-count adds.
    stats2 = await enqueue_seeded_offline(
        database=database,
        pan=pan,
        discover=discover,  # type: ignore[arg-type]
        work_ids=[work_id],
        pace_seconds=0,
        data_dir=data_dir,
    )
    assert stats2.hunting == 1
    assert stats2.want_list_added == 0
    prefs2 = LibraryPrefsStore(data_dir / "library-prefs.json").load()
    assert prefs2.want_list.count(work_id) == 1


def test_promote_hunting_to_want_list_ignores_skipped(tmp_path: Path) -> None:
    from shadow_mdc.services.library_prefs import LibraryPrefsStore
    from shadow_mdc.services.seed_auto_offline import (
        SeedOfflineStats,
        promote_hunting_to_want_list,
    )

    store = LibraryPrefsStore(tmp_path / "library-prefs.json")
    stats = SeedOfflineStats()
    stats.record("a", "A-1", "hunting", "no magnets")
    stats.record("b", "B-1", "skipped", "pan not ready")
    stats.record("c", "C-1", "submitted", "hash")
    added = promote_hunting_to_want_list(store, stats)
    assert added == 1
    assert store.load().want_list == ["a"]
    assert stats.want_list_added == 1
