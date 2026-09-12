"""Persistent user library preferences: want-list, actor tags, subscriptions, queue."""

from __future__ import annotations

import json
import uuid
from datetime import date, datetime, timezone
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

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


class LibraryPrefsStore:
    def __init__(self, path: Path) -> None:
        self.path = path

    def load(self) -> LibraryPrefs:
        if not self.path.is_file():
            return LibraryPrefs()
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        return LibraryPrefs.model_validate(payload)

    def save(self, prefs: LibraryPrefs) -> LibraryPrefs:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            prefs.model_dump_json(indent=2),
            encoding="utf-8",
        )
        return prefs

    def set_want(self, work_id: str, wanted: bool) -> LibraryPrefs:
        prefs = self.load()
        ids = [item for item in prefs.want_list if item != work_id]
        if wanted:
            ids.append(work_id)
        return self.save(prefs.model_copy(update={"want_list": ids}))

    def set_actor_tags(self, actor_key: str, tags: ActorTagState) -> LibraryPrefs:
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
        prefs = self.load()
        items = [item for item in prefs.subscriptions if item.actor_key != subscription.actor_key]
        now = datetime.now(timezone.utc).isoformat()
        subscription = subscription.model_copy(update={"updated_at": now})
        items.append(subscription)
        # Keep actor tag subscribe in sync
        tags = dict(prefs.actor_tags)
        current = tags.get(subscription.actor_key, ActorTagState())
        tags[subscription.actor_key] = current.model_copy(
            update={"subscribe": subscription.enabled, "blacklist": False if subscription.enabled else current.blacklist}
        )
        return self.save(prefs.model_copy(update={"subscriptions": items, "actor_tags": tags}))

    def remove_subscription(self, actor_key: str) -> LibraryPrefs:
        prefs = self.load()
        items = [item for item in prefs.subscriptions if item.actor_key != actor_key]
        tags = dict(prefs.actor_tags)
        if actor_key in tags:
            tags[actor_key] = tags[actor_key].model_copy(update={"subscribe": False})
            if not tags[actor_key].favorite and not tags[actor_key].blacklist:
                tags.pop(actor_key, None)
        return self.save(prefs.model_copy(update={"subscriptions": items, "actor_tags": tags}))

    def set_queue(self, items: list[SubscriptionQueueItem]) -> LibraryPrefs:
        prefs = self.load()
        return self.save(prefs.model_copy(update={"queue": items}))

    def append_queue(self, items: list[SubscriptionQueueItem]) -> LibraryPrefs:
        prefs = self.load()
        merged = merge_queue_items(list(prefs.queue), items)
        return self.save(prefs.model_copy(update={"queue": merged}))

    def update_queue_item(self, item_id: str, status: str) -> LibraryPrefs:
        prefs = self.load()
        queue = [
            item.model_copy(update={"status": status}) if item.id == item_id else item
            for item in prefs.queue
        ]
        return self.save(prefs.model_copy(update={"queue": queue}))


def new_queue_id() -> str:
    return str(uuid.uuid4())


def today_utc() -> date:
    return datetime.now(timezone.utc).date()
