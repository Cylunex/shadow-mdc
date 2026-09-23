"""Subscription auto-watch → 115 offline (mocked pan; no real 115 calls)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import WorkMagnet
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.library_prefs import (
    ActorSubscription,
    LibraryPrefs,
    LibraryPrefsStore,
    SubscriptionQueueItem,
)
from shadow_mdc.services.pan_offline_enqueue import pick_best_magnet
from shadow_mdc.services.subscription_watch import collect_watch_work_ids


def test_pick_best_magnet_prefers_subtitle_then_hd() -> None:
    plain = WorkMagnet(
        work_id="w",
        provider="javdb",
        info_hash="A" * 40,
        uri="magnet:?xt=urn:btih:" + "A" * 40,
        has_subtitle=False,
        hd=True,
        size_bytes=100,
    )
    sub = WorkMagnet(
        work_id="w",
        provider="javdb",
        info_hash="B" * 40,
        uri="magnet:?xt=urn:btih:" + "B" * 40,
        has_subtitle=True,
        hd=False,
        size_bytes=10,
    )
    assert pick_best_magnet([plain, sub]) is sub
    assert pick_best_magnet([]) is None


def test_collect_watch_work_ids_includes_want_accepted_and_matches(tmp_path: Path) -> None:
    db_path = tmp_path / "t.db"
    database = Database(f"sqlite:///{db_path}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        wanted = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="want1",
                title="Wanted",
                code="WANT-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/want1",
                actors=["Solo"],
                release_date=date(2026, 1, 1),
            ),
            overwrite=True,
        )
        accepted = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="acc1",
                title="Accepted",
                code="ACC-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/acc1",
                actors=["Star A"],
                release_date=date(2026, 2, 1),
            ),
            overwrite=True,
        )
        matched = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="sub1",
                title="Sub Match",
                code="SUB-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/sub1",
                actors=["Star A"],
                release_date=date(2026, 3, 1),
            ),
            overwrite=True,
        )
        dismissed = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="dis1",
                title="Dismissed",
                code="DIS-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/dis1",
                actors=["Star A"],
                release_date=date(2026, 4, 1),
            ),
            overwrite=True,
        )
        repo.sync_all_work_actors()
        prefs = LibraryPrefs(
            want_list=[wanted.id],
            subscriptions=[
                ActorSubscription(
                    actor_key="star-a",
                    actor_name="Star A",
                    start_date=date(2026, 1, 1),
                    max_cast=3,
                    enabled=True,
                )
            ],
            queue=[
                SubscriptionQueueItem(
                    id="q1",
                    actor_key="star-a",
                    actor_name="Star A",
                    work_id=accepted.id,
                    code="ACC-001",
                    title="Accepted",
                    status="accepted",
                ),
                SubscriptionQueueItem(
                    id="q2",
                    actor_key="star-a",
                    actor_name="Star A",
                    work_id=dismissed.id,
                    code="DIS-001",
                    title="Dismissed",
                    status="dismissed",
                ),
            ],
        )
        ids = set(collect_watch_work_ids(prefs, repo))
        assert wanted.id in ids
        assert accepted.id in ids
        assert matched.id in ids
        assert dismissed.id not in ids


def _boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}")
    monkeypatch.setenv("SHADOW_MDC_TRANSLATION_ENABLED", "false")
    monkeypatch.setenv("SHADOW_MDC_AUTO_SEED_NON_JAV_WORKS", "false")
    return TestClient(app)


@pytest.mark.asyncio
async def test_watch_run_once_enqueues_offline_for_want_work(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _boot(tmp_path, monkeypatch) as client:
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="auto1",
                    title="Auto Offline",
                    code="AUTO-001",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    source_url="https://javdb.com/v/auto1",
                    actors=["Star A"],
                    release_date=date(2026, 5, 1),
                ),
                overwrite=True,
            )
            repo.save_work_magnets(
                work,
                [
                    {
                        "uri": "magnet:?xt=urn:btih:" + "C" * 40,
                        "info_hash": "C" * 40,
                        "name": "AUTO-001",
                        "has_subtitle": True,
                        "hd": True,
                    }
                ],
                provider="javdb",
            )
            work_id = work.id

        prefs_store: LibraryPrefsStore = app.state.runtime.library_prefs_store
        prefs_store.set_want(work_id, True)

        pan = app.state.runtime.pan_service
        pan.save_settings(
            {
                "offline_directory_id": "dir-1",
                "subscription_auto_offline": True,
            }
        )

        async def fake_submit(
            url: str, *, directory_id: str, info_hash_hint: str | None = None
        ):
            return {"info_hash": (info_hash_hint or "C" * 40).upper()}

        monkeypatch.setattr(pan, "status", lambda: {"connected": True, "configured": True})
        monkeypatch.setattr(pan, "submit_offline_url", AsyncMock(side_effect=fake_submit))

        status = await app.state.runtime.subscription_watch_poller.service.run_once()
        assert status.enabled is True
        assert status.last_targets >= 1
        assert status.last_submitted >= 1

        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            tasks = repo.list_pan_offline_tasks(work_id=work_id)
            assert len(tasks) >= 1
            assert tasks[0].info_hash == "C" * 40
            assert tasks[0].directory_id == "dir-1"

        settings = client.get("/api/pan/settings")
        assert settings.status_code == 200
        assert settings.json()["subscription_auto_offline"] is True
        watch = client.get("/api/pan/subscription-watch/status")
        assert watch.status_code == 200
        assert watch.json()["enabled"] is True


@pytest.mark.asyncio
async def test_watch_skips_offline_when_pan_disconnected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    with _boot(tmp_path, monkeypatch) as client:
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="skip1",
                    title="Skip Offline",
                    code="SKIP-001",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    source_url="https://javdb.com/v/skip1",
                    release_date=date(2026, 6, 1),
                ),
                overwrite=True,
            )
            repo.save_work_magnets(
                work,
                [
                    {
                        "uri": "magnet:?xt=urn:btih:" + "D" * 40,
                        "info_hash": "D" * 40,
                        "has_subtitle": False,
                        "hd": True,
                    }
                ],
                provider="javdb",
            )
            work_id = work.id

        app.state.runtime.library_prefs_store.set_want(work_id, True)
        pan = app.state.runtime.pan_service
        pan.save_settings(
            {
                "offline_directory_id": "dir-1",
                "subscription_auto_offline": True,
            }
        )
        monkeypatch.setattr(pan, "status", lambda: {"connected": False, "configured": False})
        submit = AsyncMock()
        monkeypatch.setattr(pan, "submit_offline_url", submit)

        status = await app.state.runtime.subscription_watch_poller.service.run_once()
        assert status.last_skipped_pan >= 1
        assert status.last_submitted == 0
        submit.assert_not_awaited()

        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            assert repo.list_work_magnets(work_id)
            assert repo.list_pan_offline_tasks(work_id=work_id) == []

        body = pan.settings_status()
        disabled = client.put(
            "/api/pan/settings",
            json={
                "offline_directory_id": "dir-1",
                "strm_enabled": False,
                "strm_output_root": None,
                "strm_url_prefix": body["strm_url_prefix"],
                "use_proxy": False,
                "subscription_auto_offline": False,
                "client_id": body["client_id"],
            },
        )
        assert disabled.status_code == 200
        assert disabled.json()["subscription_auto_offline"] is False
