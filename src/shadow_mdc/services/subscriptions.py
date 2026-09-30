"""Actor subscription helpers: cast-size filter and new-works queue items."""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, date, datetime, timedelta

from pydantic import BaseModel, ConfigDict, Field


class ActorSubscription(BaseModel):
    model_config = ConfigDict(extra="forbid")

    actor_key: str
    actor_name: str
    start_date: date
    max_cast: int = Field(default=3, ge=1, le=50)
    enabled: bool = True
    notes: str | None = None
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    updated_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    # Cursor set once when the subscription is created (or first seen by the
    # watcher). It may reach back before start_date so a debut work, or a
    # release from the days just before subscribing, is not dropped.
    cursor_date: date | None = None
    cursor_initialized: bool = False

    def effective_start_date(self) -> date:
        if self.cursor_date is not None and self.cursor_date < self.start_date:
            return self.cursor_date
        return self.start_date


# Releases this many days before start_date still count as "new" for a fresh subscription.
NEW_SUBSCRIPTION_GRACE_DAYS = 7
# An actor whose every known work falls within this window is treated as a debutante.
DEBUT_WINDOW_DAYS = 120


def _as_date(value: object) -> date | None:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, str) and value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            return None
    return None


def initial_cursor_date(start_date: date, release_dates: Iterable[object]) -> date:
    """Pick the first cursor for a new actor subscription.

    * default: ``start_date - NEW_SUBSCRIPTION_GRACE_DAYS``
    * debut actor (all known dated works within ``DEBUT_WINDOW_DAYS`` of
      start_date): reach back to the earliest work so the debut is included.
    """

    cursor = start_date - timedelta(days=NEW_SUBSCRIPTION_GRACE_DAYS)
    dated = sorted(item for item in (_as_date(value) for value in release_dates) if item is not None)
    if dated and dated[0] >= start_date - timedelta(days=DEBUT_WINDOW_DAYS):
        cursor = min(cursor, dated[0])
    return cursor


def initialize_subscription_cursor(
    subscription: ActorSubscription, release_dates: Iterable[object]
) -> ActorSubscription:
    if subscription.cursor_initialized:
        return subscription
    return subscription.model_copy(
        update={
            "cursor_date": initial_cursor_date(subscription.start_date, release_dates),
            "cursor_initialized": True,
        }
    )


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
    created_at: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())


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
