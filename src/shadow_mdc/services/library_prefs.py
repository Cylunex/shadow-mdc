"""Persistent user library preferences: want-list, actor tags, subscriptions, queue."""

from __future__ import annotations

import json
import uuid
from datetime import UTC, date, datetime
from pathlib import Path
from threading import Lock, RLock
from weakref import WeakValueDictionary

from pydantic import BaseModel, ConfigDict, Field

from ..media.atomicfile import write_text
from .subscriptions import ActorSubscription, SubscriptionQueueItem, merge_queue_items


class ActorTagState(BaseModel):
    model_config = ConfigDict(extra="forbid")

    favorite: bool = False
    subscribe: bool = False
    blacklist: bool = False


class LibraryPrefs(BaseModel):
    model_config = ConfigDict(extra="forbid")

    want_list: list[str] = Field(default_factory=list)
    actor_tags: dict[str, ActorTagState] = Field(default_factory=dict)
    subscriptions: list[ActorSubscription] = Field(default_factory=list)
    queue: list[SubscriptionQueueItem] = Field(default_factory=list)


_store_locks: WeakValueDictionary[Path, RLock] = WeakValueDictionary()
_store_locks_guard = Lock()


class LibraryPrefsStore:
    def __init__(self, path: Path) -> None:
        self.path = path.resolve()
        # Separate instances for one file must share the read-modify-write lock.
        with _store_locks_guard:
            self._lock = _store_locks.setdefault(self.path, RLock())

    def load(self) -> LibraryPrefs:
        with self._lock:
            if not self.path.is_file():
                return LibraryPrefs()
            payload = json.loads(self.path.read_text(encoding="utf-8"))
            return LibraryPrefs.model_validate(payload)

    def save(self, prefs: LibraryPrefs) -> LibraryPrefs:
        with self._lock:
            write_text(self.path, prefs.model_dump_json(indent=2))
            return prefs

    def set_want(self, work_id: str, wanted: bool) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            ids = [item for item in prefs.want_list if item != work_id]
            if wanted:
                ids.append(work_id)
            return self.save(prefs.model_copy(update={"want_list": ids}))

    def add_wants(self, work_ids: list[str]) -> LibraryPrefs:
        """Append work ids to want_list (idempotent; preserves order)."""
        with self._lock:
            prefs = self.load()
            ids = list(prefs.want_list)
            seen = set(ids)
            changed = False
            for work_id in work_ids:
                if not work_id or work_id in seen:
                    continue
                ids.append(work_id)
                seen.add(work_id)
                changed = True
            if not changed:
                return prefs
            return self.save(prefs.model_copy(update={"want_list": ids}))

    def set_actor_tags(self, actor_key: str, tags: ActorTagState) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            actor_tags = dict(prefs.actor_tags)
            if not tags.favorite and not tags.subscribe and not tags.blacklist:
                actor_tags.pop(actor_key, None)
            else:
                # blacklist clears subscribe/favorite mutually
                if tags.blacklist:
                    tags = ActorTagState(favorite=False, subscribe=False, blacklist=True)
                actor_tags[actor_key] = tags
            return self.save(prefs.model_copy(update={"actor_tags": actor_tags}))

    def upsert_subscription(self, subscription: ActorSubscription) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            previous = next(
                (item for item in prefs.subscriptions if item.actor_key == subscription.actor_key), None
            )
            items = [item for item in prefs.subscriptions if item.actor_key != subscription.actor_key]
            now = datetime.now(UTC).isoformat()
            update: dict[str, object] = {"updated_at": now}
            if previous is not None and previous.cursor_initialized and not subscription.cursor_initialized:
                # Edits (enable/max_cast/notes) keep the cursor chosen at creation.
                update.update(
                    cursor_date=previous.cursor_date,
                    cursor_initialized=True,
                    created_at=previous.created_at,
                )
            subscription = subscription.model_copy(update=update)
            items.append(subscription)
            # Keep actor tag subscribe in sync
            tags = dict(prefs.actor_tags)
            current = tags.get(subscription.actor_key, ActorTagState())
            tags[subscription.actor_key] = current.model_copy(
                update={
                    "subscribe": subscription.enabled,
                    "blacklist": False if subscription.enabled else current.blacklist,
                }
            )
            return self.save(prefs.model_copy(update={"subscriptions": items, "actor_tags": tags}))

    def replace_subscriptions(self, subscriptions: list[ActorSubscription]) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            return self.save(prefs.model_copy(update={"subscriptions": subscriptions}))

    def remove_subscription(self, actor_key: str) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            items = [item for item in prefs.subscriptions if item.actor_key != actor_key]
            tags = dict(prefs.actor_tags)
            if actor_key in tags:
                tags[actor_key] = tags[actor_key].model_copy(update={"subscribe": False})
                if not tags[actor_key].favorite and not tags[actor_key].blacklist:
                    tags.pop(actor_key, None)
            return self.save(prefs.model_copy(update={"subscriptions": items, "actor_tags": tags}))

    def set_queue(self, items: list[SubscriptionQueueItem]) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            return self.save(prefs.model_copy(update={"queue": items}))

    def append_queue(self, items: list[SubscriptionQueueItem]) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            merged = merge_queue_items(list(prefs.queue), items)
            return self.save(prefs.model_copy(update={"queue": merged}))

    def update_queue_item(self, item_id: str, status: str) -> LibraryPrefs:
        with self._lock:
            prefs = self.load()
            queue = [
                item.model_copy(update={"status": status}) if item.id == item_id else item
                for item in prefs.queue
            ]
            return self.save(prefs.model_copy(update={"queue": queue}))


def new_queue_id() -> str:
    return str(uuid.uuid4())


def today_utc() -> date:
    return datetime.now(UTC).date()
