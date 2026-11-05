"""Herald-style subscription seen-key (番号) dedup alongside date cursor."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.library_prefs import LibraryPrefsStore
from shadow_mdc.services.subscriptions import (
    ActorSubscription,
    SubscriptionQueueItem,
    filter_unseen_works,
    merge_seen_codes,
    subscription_item_key,
)


def test_subscription_item_key_prefers_code() -> None:
    assert subscription_item_key(code="ABC-123", title="Ignored") == "abc-123"
    assert subscription_item_key(code="  Abc-123 ", title="x") == "abc-123"
    assert subscription_item_key(code=None, title="Solo Title") == "t:solo title"


def test_filter_unseen_skips_known_codes() -> None:
    works = [
        {"code": "A-1", "title": "One"},
        {"code": "B-2", "title": "Two"},
        {"code": "A-1", "title": "Dup"},
    ]
    fresh, keys = filter_unseen_works(works, seen_codes=["a-1"])
    assert [item["code"] for item in fresh] == ["B-2"]
    assert keys == ["b-2"]


def test_merge_seen_codes_dedupes_casefold() -> None:
    assert merge_seen_codes(["ABC-1", "x"], ["abc-1", "Y"]) == ["abc-1", "x", "y"]


def test_seen_codes_survive_subscription_edit(tmp_path: Path) -> None:
    store = LibraryPrefsStore(tmp_path / "prefs.json")
    store.upsert_subscription(
        ActorSubscription(
            actor_key="a",
            actor_name="A",
            start_date=date(2026, 9, 30),
            cursor_initialized=True,
            cursor_date=date(2026, 9, 20),
            seen_codes=["ssis-001"],
        )
    )
    store.upsert_subscription(
        ActorSubscription(actor_key="a", actor_name="A", start_date=date(2026, 9, 30), max_cast=5)
    )
    saved = store.load().subscriptions[0]
    assert saved.seen_codes == ["ssis-001"]
    assert saved.max_cast == 5
    assert saved.cursor_initialized is True


def test_append_queue_and_mark_seen_persists(tmp_path: Path) -> None:
    store = LibraryPrefsStore(tmp_path / "prefs.json")
    store.upsert_subscription(
        ActorSubscription(actor_key="a", actor_name="A", start_date=date(2026, 1, 1))
    )
    item = SubscriptionQueueItem(
        id="1",
        actor_key="a",
        actor_name="A",
        code="NEW-001",
        title="New",
        status="pending",
    )
    prefs = store.append_queue_and_mark_seen([item], {"a": ["new-001"]})
    assert len(prefs.queue) == 1
    assert prefs.subscriptions[0].seen_codes == ["new-001"]

    # Second pass with same code must not grow the queue even if date still matches.
    again = store.append_queue_and_mark_seen(
        [
            SubscriptionQueueItem(
                id="2",
                actor_key="a",
                actor_name="A",
                code="NEW-001",
                title="New again",
                status="pending",
            )
        ],
        {"a": ["new-001"]},
    )
    assert len(again.queue) == 1
    assert again.queue[0].status == "pending"


def test_scan_skips_seen_after_queue_cleared(tmp_path: Path) -> None:
    """Once a 番号 was queued, clearing the queue must not re-notify on rescan."""

    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    prefs_path = tmp_path / "prefs.json"
    store = LibraryPrefsStore(prefs_path)
    with database.session() as session:
        repo = Repository(session)
        first = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="seen1",
                title="Seen",
                code="SEEN-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/seen1",
                actors=["Star A"],
                release_date=date(2026, 10, 1),
            ),
            overwrite=True,
        )
        second = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="fresh1",
                title="Fresh",
                code="FRESH-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/fresh1",
                actors=["Star A"],
                release_date=date(2026, 10, 2),
            ),
            overwrite=True,
        )
        repo.sync_all_work_actors()
        assert first.id and second.id

    store.upsert_subscription(
        ActorSubscription(
            actor_key="star-a",
            actor_name="Star A",
            start_date=date(2026, 9, 1),
            cursor_initialized=True,
            cursor_date=date(2026, 9, 1),
        )
    )

    # Point the app at this temp store/db via monkeypatch-style override is heavy;
    # exercise the store path used by scan directly instead.
    prefs = store.load()
    from collections import defaultdict

    from shadow_mdc.services.subscriptions import (
        filter_works_for_subscription,
        filter_unseen_works,
        merge_seen_codes,
        queue_item_seen_key,
    )

    with database.session() as session:
        repo = Repository(session)
        by_actor: dict[str, list] = defaultdict(list)
        for actor, work in repo.list_actor_work_relations():
            by_actor[actor.name].append(work)
            by_actor[actor.id].append(work)
        sub = prefs.subscriptions[0]
        works = by_actor.get(sub.actor_name) or []
        payloads = [
            {
                "release_date": w.release_date,
                "actors": list(w.actors or []),
                "title": w.title,
                "code": w.primary_code,
                "work_id": w.id,
                "cast_count": len(w.actors or []),
            }
            for w in works
        ]
        matched = filter_works_for_subscription(
            payloads, start_date=sub.effective_start_date(), max_cast=sub.max_cast
        )
        known = merge_seen_codes(sub.seen_codes, [])
        fresh, keys = filter_unseen_works(matched, seen_codes=known)
        assert sorted(item["code"] for item in fresh) == ["FRESH-001", "SEEN-001"]
        store.append_queue_and_mark_seen(
            [
                SubscriptionQueueItem(
                    id="q1",
                    actor_key=sub.actor_key,
                    actor_name=sub.actor_name,
                    code=str(item["code"]),
                    title=str(item["title"]),
                    work_id=str(item["work_id"]),
                    status="pending",
                )
                for item in fresh
            ],
            {sub.actor_key: merge_seen_codes(known, keys)},
        )

    # User clears the queue after dismissing; seen_codes must still block re-queue.
    store.set_queue([])
    prefs = store.load()
    assert prefs.subscriptions[0].seen_codes == ["fresh-001", "seen-001"] or set(
        prefs.subscriptions[0].seen_codes
    ) == {"fresh-001", "seen-001"}
    with database.session() as session:
        repo = Repository(session)
        by_actor = defaultdict(list)
        for actor, work in repo.list_actor_work_relations():
            by_actor[actor.name].append(work)
        sub = prefs.subscriptions[0]
        works = by_actor.get(sub.actor_name) or []
        payloads = [
            {
                "release_date": w.release_date,
                "actors": list(w.actors or []),
                "title": w.title,
                "code": w.primary_code,
                "work_id": w.id,
                "cast_count": len(w.actors or []),
            }
            for w in works
        ]
        matched = filter_works_for_subscription(
            payloads, start_date=sub.effective_start_date(), max_cast=sub.max_cast
        )
        known = merge_seen_codes(
            sub.seen_codes, [queue_item_seen_key(i) for i in prefs.queue]
        )
        fresh, _ = filter_unseen_works(matched, seen_codes=known)
        assert fresh == []
