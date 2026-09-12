"""Unit tests for status badges, non-JAV studio guard, and subscription filters."""

from __future__ import annotations

from datetime import date

from shadow_mdc.services.studio_guard import (
    classify_non_jav_actor_rejection,
    reject_non_jav_studio_label,
)
from shadow_mdc.services.subscriptions import (
    cast_within_limit,
    filter_works_for_subscription,
    merge_queue_items,
    SubscriptionQueueItem,
)
from shadow_mdc.services.work_status import compute_work_status, status_badges


def test_status_badge_logic() -> None:
    flags = compute_work_status(want_list=True, in_catalog=True, has_local_media=False, emby_linked=True)
    assert status_badges(flags) == ("想看", "已入库", "有本地或Emby")
    empty = compute_work_status(want_list=False, in_catalog=False, has_local_media=False)
    assert status_badges(empty) == ()
    catalog_only = compute_work_status(want_list=False, in_catalog=True, has_local_media=False)
    assert status_badges(catalog_only) == ("已入库",)


def test_non_jav_studio_guard() -> None:
    assert classify_non_jav_actor_rejection("麻豆传媒") == "studio_label"
    assert classify_non_jav_actor_rejection("91探花") == "studio_label"
    assert classify_non_jav_actor_rejection("北京探花") == "studio_label"
    assert classify_non_jav_actor_rejection("约炮少妇") == "studio_label"
    assert classify_non_jav_actor_rejection("未知") == "fuzzy"
    assert classify_non_jav_actor_rejection("蜜桃酱") is None
    assert reject_non_jav_studio_label("果冻传媒") is not None
    assert reject_non_jav_studio_label("三上悠亚") is None


def test_subscription_filter_helpers() -> None:
    assert cast_within_limit(3, 3) is True
    assert cast_within_limit(4, 3) is False
    works = [
        {"title": "A", "release_date": "2026-01-01", "actors": ["x"], "code": "A-1"},
        {"title": "B", "release_date": "2025-01-01", "actors": ["x"], "code": "B-1"},
        {"title": "C", "release_date": "2026-06-01", "actors": ["a", "b", "c", "d"], "code": "C-1"},
    ]
    kept = filter_works_for_subscription(works, start_date=date(2026, 1, 1), max_cast=3)
    assert [item["code"] for item in kept] == ["A-1"]
    existing = [
        SubscriptionQueueItem(
            id="1",
            actor_key="a1",
            actor_name="A",
            title="Same",
            code="X-1",
            status="accepted",
        )
    ]
    incoming = [
        SubscriptionQueueItem(
            id="2",
            actor_key="a1",
            actor_name="A",
            title="Same",
            code="X-1",
            status="pending",
        ),
        SubscriptionQueueItem(
            id="3",
            actor_key="a1",
            actor_name="A",
            title="New",
            code="Y-1",
            status="pending",
        ),
    ]
    merged = merge_queue_items(existing, incoming)
    assert len(merged) == 2
    assert next(item for item in merged if item.code == "X-1").status == "accepted"
