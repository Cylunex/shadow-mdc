"""Actor subscription helpers: cast-size filter and new-works queue items."""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import Iterable

from pydantic import BaseModel, ConfigDict, Field


class ActorSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_key: str
    actor_name: str
    start_date: date
    max_cast: int = Field(default=3, ge=1, le=50)
    enabled: bool = True
    notes: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class SubscriptionQueueItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    actor_key: str
    actor_name: str
    work_id: str | None = None
    code: str | None = None
    title: str
    release_date: date | None = None
    cast_count: int = 0
    status: str = "pending"  # pending | accepted | dismissed
    created_at: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


def cast_within_limit(cast_count: int, max_cast: int) -> bool:
    """True when a work's billed cast size is allowed by the subscription."""

    if max_cast < 1:
        return False
    return 0 <= cast_count <= max_cast


def filter_works_for_subscription(
    works: Iterable[dict[str, object]],
    *,
    start_date: date,
    max_cast: int,
) -> list[dict[str, object]]:
    """Keep works released on/after start_date with cast size ≤ max_cast."""

    kept: list[dict[str, object]] = []
    for work in works:
        release = work.get("release_date")
        release_value: date | None
        if isinstance(release, date):
            release_value = release
        elif isinstance(release, str) and release:
            release_value = date.fromisoformat(release[:10])
        else:
            release_value = None
        if release_value is not None and release_value < start_date:
            continue
        actors = work.get("actors")
        if isinstance(actors, (list, tuple)):
            cast_count = len(actors)
        else:
            cast_count = int(work.get("cast_count") or 0)
        if not cast_within_limit(cast_count, max_cast):
            continue
        kept.append(work)
    return kept


def merge_queue_items(
    existing: list[SubscriptionQueueItem],
    incoming: list[SubscriptionQueueItem],
) -> list[SubscriptionQueueItem]:
    """Deduplicate queue by (actor_key, code|title), prefer existing status."""

    index: dict[str, SubscriptionQueueItem] = {}
    for item in existing:
        key = f"{item.actor_key}|{(item.code or item.title).casefold()}"
        index[key] = item
    for item in incoming:
        key = f"{item.actor_key}|{(item.code or item.title).casefold()}"
        if key not in index:
            index[key] = item
    return sorted(index.values(), key=lambda item: item.created_at, reverse=True)
