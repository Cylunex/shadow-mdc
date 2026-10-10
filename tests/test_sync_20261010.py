"""Sync 2026-10-10: magnet failover, STRM signed-URL cache, drive read-after-check."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import WorkMagnet
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.offline_control import OfflineRecoveryController
from shadow_mdc.services.offline_recovery import (
    DEFAULT_POLICY,
    SWITCH_REASON_FAILED,
    SWITCH_REASON_STALLED,
    after_submit,
    begin_switch,
    detect_stall_or_fail,
    load_recovery,
    next_magnet_candidate,
    observe_progress,
    project_offline,
    seed_recovery,
    stream_cache_expiry,
)
from shadow_mdc.services.pan import PanService, SourceChangedError, SourceFingerprint
from shadow_mdc.services.pan_common import SingleFlight
from shadow_mdc.services.strm_relay import StrmRelay


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    return database


def _work_with_magnets(repo: Repository, *, count: int = 3) -> tuple[str, list[WorkMagnet]]:
    work = repo.upsert_provider_record(
        ProviderRecord(
            provider="javdb",
            external_id="failover1",
            title="FO-1",
            code="FO-1",
            family=ContentFamily.JAV,
            category=MediaCategory.JAPAN,
            source_url="https://javdb.com/v/failover1",
        ),
        overwrite=True,
    )
    payload = [
        {
            "uri": f"magnet:?xt=urn:btih:{'A' * 39}{index}&dn=FO-1-{index}",
            "info_hash": f"{'A' * 39}{index}",
            "name": f"FO-1-{index}.mp4",
            "size_bytes": 1_000_000_000 - index * 10_000,
            "has_subtitle": index == 0,
            "hd": True,
        }
        for index in range(count)
    ]
    repo.save_work_magnets(work, payload, provider="javdb")
    magnets = repo.list_work_magnets(work.id)
    # Stable order by info_hash for assertions.
    magnets.sort(key=lambda item: item.info_hash or "")
    return work.id, magnets


# ------------------------------------------------------------- recovery core


def test_seed_and_project_offline_task(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        repo = Repository(session)
        work_id, magnets = _work_with_magnets(repo, count=2)
        recovery = seed_recovery(
            info_hash=magnets[0].info_hash or "",
            magnet_id=magnets[0].id,
            url=magnets[0].uri,
        )
        task = repo.create_pan_offline_task(
            work_id=work_id,
            info_hash=magnets[0].info_hash or "X",
            directory_id="/115/云下载",
            url=magnets[0].uri,
            magnet_id=magnets[0].id,
            backend="openlist",
            remote_task_id="tid-1",
            recovery_json=recovery.to_dict(),
        )
        projection = project_offline(task)
        assert projection.attempt_count == 1
        assert projection.can_cancel is True
        assert projection.can_switch is True
        assert projection.download_state == "queued"


def test_detect_stall_zero_progress_and_grace(tmp_path: Path) -> None:
    start = datetime(2026, 10, 10, 10, 0, tzinfo=UTC)
    state = seed_recovery(
        info_hash="AA", magnet_id=None, url="magnet:?xt=urn:btih:AA", started_at=start
    )
    observe_progress(state, progress=0.0, clock=start)
    # Fresh poll observation (observed_at recent) while progress_at stays at start.
    early = start + timedelta(minutes=14)
    observe_progress(state, progress=0.0, clock=early)
    assert (
        detect_stall_or_fail(
            state,
            remote_failed=False,
            remote_running=True,
            auto_switch=True,
            clock=early,
        )
        is None
    )
    late = start + timedelta(minutes=16)
    observe_progress(state, progress=0.0, clock=late)
    assert (
        detect_stall_or_fail(
            state,
            remote_failed=False,
            remote_running=True,
            auto_switch=True,
            clock=late,
        )
        == SWITCH_REASON_STALLED
    )
    state2 = seed_recovery(
        info_hash="BB", magnet_id=None, url="magnet:?xt=urn:btih:BB", started_at=start
    )
    observe_progress(state2, progress=96.0, clock=start)
    mid = start + timedelta(minutes=50)
    observe_progress(state2, progress=96.0, clock=mid)
    assert (
        detect_stall_or_fail(
            state2,
            remote_failed=False,
            remote_running=True,
            auto_switch=True,
            clock=mid,
        )
        is None
    )
    grace = start + timedelta(minutes=61)
    observe_progress(state2, progress=96.0, clock=grace)
    assert (
        detect_stall_or_fail(
            state2,
            remote_failed=False,
            remote_running=True,
            auto_switch=True,
            clock=grace,
        )
        == SWITCH_REASON_STALLED
    )


def test_next_magnet_skips_attempted_and_caps_at_three(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with database.session() as session:
        repo = Repository(session)
        _work_id, magnets = _work_with_magnets(repo, count=4)
        state = seed_recovery(
            info_hash=magnets[0].info_hash or "", magnet_id=magnets[0].id, url=magnets[0].uri
        )
        nxt = next_magnet_candidate(magnets, state)
        assert nxt is not None and nxt.id != magnets[0].id
        state = begin_switch(state, reason=SWITCH_REASON_FAILED, next_magnet=nxt)
        state = after_submit(
            state, info_hash=nxt.info_hash or "", magnet_id=nxt.id, url=nxt.uri
        )
        assert len(state.attempts) == 2
        # Exhaust after max attempts worth of used hashes.
        while len(state.attempts) < DEFAULT_POLICY.max_attempts:
            candidate = next_magnet_candidate(magnets, state)
            assert candidate is not None
            state = begin_switch(state, reason=SWITCH_REASON_FAILED, next_magnet=candidate)
            state = after_submit(
                state,
                info_hash=candidate.info_hash or "",
                magnet_id=candidate.id,
                url=candidate.uri,
            )
        assert next_magnet_candidate(magnets, state) is None


def test_failover_removes_remote_before_submit(tmp_path: Path) -> None:
    database = _database(tmp_path)
    events: list[str] = []

    class _OpenList:
        async def cancel_offline_task(self, tid: str) -> None:
            events.append(f"cancel:{tid}")

        async def submit_offline(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("should go through pan.submit_offline_url")

    class _Pan:
        def __init__(self) -> None:
            self.openlist = _OpenList()
            self.config_store = SimpleNamespace(load=lambda: SimpleNamespace(auto_switch=True))

        def backend(self) -> str:
            return "openlist"

        async def submit_offline_url(self, url: str, **kwargs: Any) -> dict[str, Any]:
            events.append(f"submit:{url}")
            return {"info_hash": "BBBB", "remote_task_id": "tid-2"}

    with database.session() as session:
        repo = Repository(session)
        work_id, magnets = _work_with_magnets(repo, count=2)
        recovery = seed_recovery(
            info_hash=magnets[0].info_hash or "AAAA",
            magnet_id=magnets[0].id,
            url=magnets[0].uri,
        )
        recovery = begin_switch(
            recovery, reason=SWITCH_REASON_FAILED, next_magnet=magnets[1], manual=False
        )
        task = repo.create_pan_offline_task(
            work_id=work_id,
            info_hash=magnets[0].info_hash or "AAAA",
            directory_id="/dl",
            url=magnets[0].uri,
            magnet_id=magnets[0].id,
            backend="openlist",
            remote_task_id="tid-1",
            recovery_json=recovery.to_dict(),
        )
        task_id = task.id

    controller = OfflineRecoveryController(database=database, pan=_Pan())  # type: ignore[arg-type]
    result = asyncio.run(controller.advance(task_id))
    assert result.changed
    assert events[0] == "cancel:tid-1"
    assert events[1].startswith("submit:")
    assert events.index("cancel:tid-1") < events.index(events[1])
    with database.session() as session:
        row = Repository(session).get_pan_offline_task(task_id)
        assert row is not None
        assert row.status == "running"
        assert row.info_hash == "BBBB"
        assert row.remote_task_id == "tid-2"
        state = load_recovery(row)
        assert len(state.attempts) == 2
        assert state.action == ""


def test_cancel_marks_cancelled(tmp_path: Path) -> None:
    database = _database(tmp_path)
    events: list[str] = []

    class _OpenList:
        async def cancel_offline_task(self, tid: str) -> None:
            events.append(f"cancel:{tid}")

    class _Pan:
        def __init__(self) -> None:
            self.openlist = _OpenList()
            self.config_store = SimpleNamespace(load=lambda: SimpleNamespace(auto_switch=True))

        async def submit_offline_url(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
            raise AssertionError("cancel must not submit")

    with database.session() as session:
        repo = Repository(session)
        work_id, magnets = _work_with_magnets(repo, count=1)
        recovery = seed_recovery(
            info_hash=magnets[0].info_hash or "AAAA",
            magnet_id=magnets[0].id,
            url=magnets[0].uri,
        )
        task = repo.create_pan_offline_task(
            work_id=work_id,
            info_hash=magnets[0].info_hash or "AAAA",
            directory_id="/dl",
            url=magnets[0].uri,
            magnet_id=magnets[0].id,
            backend="openlist",
            remote_task_id="tid-9",
            recovery_json=recovery.to_dict(),
        )
        task_id = task.id

    controller = OfflineRecoveryController(database=database, pan=_Pan())  # type: ignore[arg-type]
    result = asyncio.run(controller.request_cancel(task_id))
    assert result.task.status == "cancelled"
    assert events == ["cancel:tid-9"]
    projection = project_offline(result.task)
    assert projection.can_cancel is False
    assert projection.download_state == "cancelled"


# ------------------------------------------------------------- STRM cache


def test_stream_cache_expiry_requires_known_t() -> None:
    now = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)
    assert stream_cache_expiry("https://cdn/x", now=now) is None
    future = int((now + timedelta(hours=2)).timestamp())
    expires = stream_cache_expiry(f"https://cdn/x?t={future}", now=now)
    assert expires is not None
    # Soft TTL (1m) caps long-lived signatures; still expire 30s before `t` when sooner.
    assert expires == now + timedelta(minutes=1)
    soon = int((now + timedelta(seconds=45)).timestamp())
    soon_expires = stream_cache_expiry(f"https://cdn/x?t={soon}", now=now)
    assert soon_expires == datetime.fromtimestamp(soon, tz=UTC) - timedelta(seconds=30)


def test_strm_relay_caches_signed_url_and_isolates_cancel(tmp_path: Path) -> None:
    future = int(datetime(2026, 12, 1, tzinfo=UTC).timestamp())
    calls: list[str] = []
    started = asyncio.Event()
    release = asyncio.Event()

    class _Client:
        async def get_folder_info(self, file_id: str) -> dict[str, Any]:
            return {"pick_code": f"pc{file_id}"}

        async def download_url(self, pick_code: str, *, user_agent: str) -> str:
            calls.append(f"{pick_code}:{user_agent}")
            started.set()
            await release.wait()
            return f"https://cdn.115/{pick_code}?t={future}"

        async def video_play_url(self, pick_code: str, *, user_agent: str) -> str | None:
            return None

    async def _read(factory):
        return await factory()

    pan = SimpleNamespace(
        status=lambda: {"connected": True},
        get_client=lambda: _Client(),
        config_store=SimpleNamespace(
            load=lambda: SimpleNamespace(strm_user_agent=None, strm_output_root=None)
        ),
        source_fingerprint=lambda: SourceFingerprint(
            account_id="acc", directory_id="dir", auth_version=3, backend="115_open"
        ),
        read_after_check=_read,
    )
    relay = StrmRelay(pan)  # type: ignore[arg-type]

    async def run() -> None:
        task1 = asyncio.create_task(relay.resolve("1", "UA-A"))
        await started.wait()
        task2 = asyncio.create_task(relay.resolve("1", "UA-A"))
        task1.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task1
        release.set()
        target = await task2
        assert "t=" in target.url
        # Second waiter must still get the shared resolve (not poisoned by cancel).
        assert len(calls) == 1
        # Cache hit for same key.
        again = await relay.resolve("1", "UA-A")
        assert again.url == target.url
        assert len(calls) == 1

    asyncio.run(run())


def test_singleflight_cancel_does_not_cancel_shared_work() -> None:
    flight: SingleFlight[str] = SingleFlight()
    started = asyncio.Event()
    release = asyncio.Event()
    runs = 0

    async def factory() -> str:
        nonlocal runs
        runs += 1
        started.set()
        await release.wait()
        return "ok"

    async def run() -> None:
        first = asyncio.create_task(flight.run("k", factory))
        await started.wait()
        second = asyncio.create_task(flight.run("k", factory))
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        release.set()
        assert await second == "ok"
        assert runs == 1

    asyncio.run(run())


# ------------------------------------------------------------- drive read-after-check


def test_read_after_check_discards_stale_source(tmp_path: Path) -> None:
    pan = PanService(data_dir=tmp_path / "pan-data")
    before = pan.authorization_version

    async def mutating() -> str:
        pan.bump_auth_version()
        return "stale"

    with pytest.raises(SourceChangedError):
        asyncio.run(pan.read_after_check(mutating))
    assert pan.authorization_version == before + 1

    async def stable() -> str:
        return "fresh"

    assert asyncio.run(pan.read_after_check(stable)) == "fresh"


def test_disconnect_bumps_auth_version(tmp_path: Path) -> None:
    pan = PanService(data_dir=tmp_path / "pan-data")
    version = pan.authorization_version
    pan.disconnect()
    assert pan.authorization_version == version + 1


# ------------------------------------------------------------- API surface


def test_offline_task_api_surfaces_attempt_fields(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}")
    with TestClient(app) as client:
        runtime = app.state.runtime
        with runtime.database.session() as session:
            repo = Repository(session)
            work_id, magnets = _work_with_magnets(repo, count=2)
            recovery = seed_recovery(
                info_hash=magnets[0].info_hash or "AAAA",
                magnet_id=magnets[0].id,
                url=magnets[0].uri,
            )
            task = repo.create_pan_offline_task(
                work_id=work_id,
                info_hash=magnets[0].info_hash or "AAAA",
                directory_id="/dl",
                url=magnets[0].uri,
                magnet_id=magnets[0].id,
                backend="openlist",
                recovery_json=recovery.to_dict(),
            )
            task_id = task.id
        listed = client.get("/api/pan/offline/tasks").json()
        match = next(item for item in listed if item["id"] == task_id)
        assert match["attempt_count"] == 1
        assert match["can_cancel"] is True
        assert match["download_state"] == "queued"

        # Cancel without a real OpenList: stub recovery remove.
        async def fake_remove(**kwargs: Any) -> None:
            return None

        monkeypatch.setattr(runtime.pan_poller.recovery, "_remove_remote", fake_remove)
        cancelled = client.post(f"/api/pan/offline/tasks/{task_id}/cancel")
        assert cancelled.status_code == 200
        body = cancelled.json()
        assert body["status"] == "cancelled"
        assert body["can_cancel"] is False

        settings = client.get("/api/pan/settings").json()
        assert "auto_switch" in settings
        assert settings["auto_switch"] is True

