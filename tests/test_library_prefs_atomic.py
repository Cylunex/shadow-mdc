"""Preference updates survive concurrent callers and failed file replacement."""

import os
from concurrent.futures import ThreadPoolExecutor
from datetime import date
from pathlib import Path
from time import sleep

import pytest

from shadow_mdc.services.library_prefs import ActorTagState, LibraryPrefs, LibraryPrefsStore
from shadow_mdc.services.subscriptions import ActorSubscription


class SlowReadStore(LibraryPrefsStore):
    def load(self) -> LibraryPrefs:
        prefs = super().load()
        # Widen the window where an unprotected read-modify-write loses changes.
        sleep(0.001)
        return prefs


def test_parallel_updates_share_lock_across_store_instances(tmp_path: Path) -> None:
    target = tmp_path / "prefs.json"

    def update(index: int) -> None:
        store = SlowReadStore(target)
        store.add_wants([f"work-{index}"])
        store.set_actor_tags(f"favorite-{index}", ActorTagState(favorite=True))
        store.upsert_subscription(
            ActorSubscription(actor_key=f"actor-{index}", actor_name=str(index), start_date=date.today())
        )

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(update, range(24)))

    prefs = LibraryPrefsStore(target).load()
    assert set(prefs.want_list) == {f"work-{index}" for index in range(24)}
    assert {sub.actor_key for sub in prefs.subscriptions} == {f"actor-{index}" for index in range(24)}
    assert len(prefs.actor_tags) == 48
    assert all(prefs.actor_tags[f"actor-{index}"].subscribe for index in range(24))
    assert all(prefs.actor_tags[f"favorite-{index}"].favorite for index in range(24))


def test_failed_replace_keeps_existing_preferences(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    target = tmp_path / "prefs.json"
    store = LibraryPrefsStore(target)
    store.set_want("saved", True)
    previous = target.read_bytes()

    def fail_replace(source: Path, destination: Path) -> None:
        raise OSError("simulated storage failure")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="storage failure"):
        store.set_want("unsaved", True)
    assert target.read_bytes() == previous
    assert store.load().want_list == ["saved"]
    assert sorted(item.name for item in tmp_path.iterdir()) == ["prefs.json"]


def test_atomic_save_keeps_permissions_and_valid_snapshot(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "prefs.json"
    store = LibraryPrefsStore(target)
    store.set_want("first", True)
    target.chmod(0o600)
    replace = os.replace
    observed: list[list[str]] = []

    def observe_replace(source: Path, destination: Path) -> None:
        # Readers outside the process lock still see the full old file until replacement.
        observed.append(LibraryPrefs.model_validate_json(target.read_bytes()).want_list)
        assert LibraryPrefs.model_validate_json(source.read_bytes()).want_list == ["first", "second"]
        replace(source, destination)

    monkeypatch.setattr(os, "replace", observe_replace)
    store.set_want("second", True)
    assert observed == [["first"]]
    assert store.load().want_list == ["first", "second"]
    assert target.stat().st_mode & 0o777 == 0o600
