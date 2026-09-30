"""Subscription watch checkpoint resume + actor cursor initialisation."""

from __future__ import annotations

import asyncio
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services import subscription_watch as watch_module
from shadow_mdc.services.library_prefs import LibraryPrefsStore
from shadow_mdc.services.subscription_watch import (
    SubscriptionWatchService,
    SubscriptionWatchStateStore,
    collect_watch_work_ids,
)
from shadow_mdc.services.subscriptions import (
    NEW_SUBSCRIPTION_GRACE_DAYS,
    ActorSubscription,
    initial_cursor_date,
    initialize_subscription_cursor,
)


def test_initial_cursor_default_grace_and_debut() -> None:
    start = date(2026, 9, 30)
    veteran = [date(2020, 1, 1), date(2026, 9, 1)]
    assert initial_cursor_date(start, veteran) == start - timedelta(days=NEW_SUBSCRIPTION_GRACE_DAYS)
    debut = [date(2026, 8, 20), None]
    assert initial_cursor_date(start, debut) == date(2026, 8, 20)
    assert initial_cursor_date(start, []) == start - timedelta(days=NEW_SUBSCRIPTION_GRACE_DAYS)


def test_cursor_survives_subscription_edit(tmp_path: Path) -> None:
    store = LibraryPrefsStore(tmp_path / "prefs.json")
    sub = ActorSubscription(actor_key="a", actor_name="A", start_date=date(2026, 9, 30))
    store.upsert_subscription(initialize_subscription_cursor(sub, [date(2026, 8, 1)]))
    # A plain edit from the UI (no cursor fields) must keep the creation cursor.
    store.upsert_subscription(
        ActorSubscription(actor_key="a", actor_name="A", start_date=date(2026, 9, 30), max_cast=5)
    )
    saved = store.load().subscriptions[0]
    assert saved.cursor_initialized is True
    assert saved.cursor_date == date(2026, 8, 1)
    assert saved.max_cast == 5
    assert saved.effective_start_date() == date(2026, 8, 1)


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    return database


def _work(repo: Repository, code: str, actor: str, released: date) -> str:
    work = repo.upsert_provider_record(
        ProviderRecord(
            provider="javdb",
            external_id=code.lower(),
            title=code,
            code=code,
            family=ContentFamily.JAV,
            category=MediaCategory.JAPAN,
            source_url=f"https://javdb.com/v/{code.lower()}",
            actors=[actor],
            release_date=released,
        ),
        overwrite=True,
    )
    return work.id


def test_debut_work_included_after_cursor_init(tmp_path: Path) -> None:
    database = _database(tmp_path)
    store = LibraryPrefsStore(tmp_path / "prefs.json")
    with database.session() as session:
        repo = Repository(session)
        debut_id = _work(repo, "DEB-001", "New Girl", date(2026, 9, 10))
        repo.sync_all_work_actors()
        sub = ActorSubscription(actor_key="new-girl", actor_name="New Girl", start_date=date(2026, 9, 30))
        store.upsert_subscription(sub)
        # Without a cursor the debut (before start_date) would be dropped.
        assert debut_id not in collect_watch_work_ids(store.load(), repo)
        prefs, changed = watch_module.ensure_subscription_cursors(store.load(), repo)
        assert changed is True
        assert debut_id in collect_watch_work_ids(prefs, repo)


class _FakePan:
    def __init__(self) -> None:
        self.config_store = SimpleNamespace(
            load=lambda: SimpleNamespace(subscription_auto_offline=True, offline_directory_id="d")
        )

    def status(self) -> dict[str, Any]:
        return {"connected": False}


def test_watch_resumes_from_processed_count(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    database = _database(tmp_path)
    store = LibraryPrefsStore(tmp_path / "prefs.json")
    with database.session() as session:
        repo = Repository(session)
        ids = [_work(repo, f"RES-{index:03d}", "Solo", date(2026, 1, index + 1)) for index in range(7)]
    for work_id in ids:
        store.set_want(work_id, True)
    ordered = sorted(ids)
    monkeypatch.setattr(watch_module, "WATCH_PACE_SECONDS", 0.0)

    state = SubscriptionWatchStateStore(tmp_path / "state.json")
    service = SubscriptionWatchService(
        database=database,
        pan=_FakePan(),  # type: ignore[arg-type]
        discover=SimpleNamespace(),  # type: ignore[arg-type]
        library_prefs_store=store,
        state_store=state,
    )
    processed: list[str] = []

    async def record(work_id: str, *, stats: Any, pan_ready: bool) -> None:
        processed.append(work_id)
        if len(processed) == 4:
            raise asyncio.CancelledError  # simulate a restart mid-batch

    monkeypatch.setattr(service, "_process_work", record)
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(service.run_once(limit=5))
    assert processed == ordered[:4]
    # The 4th item was interrupted before completing, so it is redone.
    assert state.load().batch_cursor == 3

    processed.clear()

    async def plain(work_id: str, *, stats: Any, pan_ready: bool) -> None:
        processed.append(work_id)

    monkeypatch.setattr(service, "_process_work", plain)
    status = asyncio.run(service.run_once(limit=5))
    assert processed == ordered[3:7]
    assert status.batch_cursor == 0  # wrapped after a full pass
    assert status.last_pass_completed_at is not None
    processed.clear()
    asyncio.run(service.run_once(limit=5))
    assert processed == ordered[:5]
    assert state.load().batch_cursor == 5
