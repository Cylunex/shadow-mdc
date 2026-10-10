"""Sync 2026-10-04: incremental export, config gate, quota split, drain, pan auth."""

from __future__ import annotations

import asyncio
import os
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.media.artwork import ArtworkStore
from shadow_mdc.services import pan as pan_module
from shadow_mdc.services import strm_export
from shadow_mdc.services import subscription_watch as watch_module
from shadow_mdc.services.library_prefs import LibraryPrefsStore
from shadow_mdc.services.media_server import MediaServerSettings, MediaServerStore
from shadow_mdc.services.pan import (
    Pan115Client,
    PanAuthRejectedError,
    PanCredentials,
    PanService,
    PanSettings,
    RemoteVideo,
)
from shadow_mdc.services.pan_common import SingleFlight
from shadow_mdc.services.pan_poller import PanOfflinePoller
from shadow_mdc.services.strm_export import (
    StrmConfigError,
    export_work,
    migrate_strm_tree,
    rewrite_strm_tree,
    strm_config_problems,
)
from shadow_mdc.services.strm_relay import StrmRelay
from shadow_mdc.services.subscription_watch import (
    SubscriptionWatchService,
    SubscriptionWatchStateStore,
    due_work_ids,
)


def _relay(root: Path | None, **extra: Any) -> PanSettings:
    values: dict[str, Any] = {
        "strm_enabled": True,
        "strm_output_root": str(root) if root else None,
        "strm_mode": "relay",
        "strm_public_base_url": "http://nas.lan:8700",
        "strm_token": "t1",
    }
    values.update(extra)
    return PanSettings(**values)


def _video(fid: str, name: str) -> RemoteVideo:
    return RemoteVideo(file_id=fid, name=name, pick_code=f"pc{fid}", relative_path=name, size=100)


def _work(art: Path) -> Any:
    (art / "poster.jpg").write_bytes(b"P" * 10)
    return SimpleNamespace(
        id="w1",
        title="T",
        original_title=None,
        primary_code="ABC-123",
        plot=None,
        release_date=date(2026, 9, 1),
        runtime_seconds=None,
        artwork=[{"kind": "poster", "local_path": str(art / "poster.jpg")}],
    )


# ------------------------------------------------- 1. content-based incremental


def test_reexport_identical_bytes_writes_nothing(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    art = tmp_path / "art"
    art.mkdir()
    work = _work(art)
    monkeypatch.setattr(strm_export, "build_nfo", lambda w, ids: "<movie><title>x</title></movie>\n")
    root = tmp_path / "emby"
    videos = [_video("11", "a.mp4")]
    first = export_work(settings=_relay(root), code="ABC-123", videos=videos, work=work)
    assert first.changed and first.created
    folder = root / "ABC-123"
    stamps = {path.name: path.stat().st_mtime_ns for path in folder.iterdir()}
    for path in folder.iterdir():
        os.utime(path, ns=(1, 1))

    writes: list[str] = []
    monkeypatch.setattr(strm_export, "_atomic_copy", lambda s, d: writes.append(f"art:{d.name}"))
    monkeypatch.setattr(strm_export, "write_nfo", lambda p, c: writes.append("nfo"))
    monkeypatch.setattr(strm_export, "write_strm", lambda p, loc: writes.append("strm"))
    second = export_work(settings=_relay(root), code="ABC-123", videos=videos, work=work)
    assert writes == []
    assert not second.changed and not second.created
    assert all(path.stat().st_mtime_ns == 1 for path in folder.iterdir())
    assert set(stamps) == {path.name for path in folder.iterdir()}

    # Token rotation: only the .strm differs → only it is written (still last).
    monkeypatch.undo()
    monkeypatch.setattr(strm_export, "build_nfo", lambda w, ids: "<movie><title>x</title></movie>\n")
    third = export_work(settings=_relay(root, strm_token="t2"), code="ABC-123", videos=videos, work=work)
    assert [path.name for path in third.changed_paths] == ["ABC-123.strm"]
    assert (folder / "poster.jpg").stat().st_mtime_ns == 1


def test_poller_skips_emby_notify_for_unchanged_export(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    pan = PanService(data_dir=data_dir)
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    cfg = _relay(tmp_path / "emby", strm_emby_root="/media/strm")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = PanOfflinePoller(
                database=database,
                pan=pan,
                media_server_store=MediaServerStore(data_dir / "ms.json"),
                http=http,
            )
            first = export_work(settings=cfg, code="ABC-1", videos=[_video("1", "a.mp4")])
            poller._notify_export(first, cfg)
            assert poller.notifier.pending == {"/media/strm/ABC-1": "Created"}
            poller.notifier._pending.clear()
            again = export_work(settings=cfg, code="ABC-1", videos=[_video("1", "a.mp4")])
            poller._notify_export(again, cfg)
            assert poller.notifier.pending == {}
            moved = export_work(settings=cfg, code="ABC-1", videos=[_video("2", "a.mp4")])
            poller._notify_export(moved, cfg)
            assert poller.notifier.pending == {"/media/strm/ABC-1": "Modified"}

    asyncio.run(scenario())


# ------------------------------------------------------- 2. hard config gate


def test_config_problems_relay_and_emby_mapping(tmp_path: Path) -> None:
    assert strm_config_problems(_relay(tmp_path)) == []
    problems = strm_config_problems(_relay(None, strm_public_base_url=None))
    assert any("strm_output_root" in item for item in problems)
    assert any("strm_public_base_url" in item for item in problems)
    assert strm_config_problems(_relay(tmp_path, strm_public_base_url="nas:8700"))
    emby = strm_config_problems(_relay(tmp_path), emby_notify=True)
    assert len(emby) == 1 and "strm_emby_root" in emby[0]
    assert strm_config_problems(_relay(tmp_path, strm_emby_root="/media"), emby_notify=True) == []


def test_rewrite_and_migrate_refuse_half_config(tmp_path: Path) -> None:
    root = tmp_path / "emby"
    export_work(settings=_relay(root), code="AAA-1", videos=[_video("1", "a.mp4")])
    strm = root / "AAA-1" / "AAA-1.strm"
    before = strm.read_text()
    with pytest.raises(StrmConfigError):
        rewrite_strm_tree(root, _relay(root, strm_public_base_url=None))
    assert strm.read_text() == before
    new_root = tmp_path / "new"
    with pytest.raises(StrmConfigError):
        migrate_strm_tree(root, new_root, _relay(new_root, strm_public_base_url=""))
    assert strm.is_file() and not new_root.exists()


def _offline_db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="abc123",
                title="ABC-123",
                code="ABC-123",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/abc123",
            ),
            overwrite=True,
        )
        repo.create_pan_offline_task(work_id=work.id, info_hash="H" * 40, directory_id="d")
    return database


class _Fake115:
    async def get_task_list(self, page: int = 1) -> dict[str, Any]:
        return {
            "page_count": 1,
            "tasks": [{"info_hash": "H" * 40, "local_status": "done", "file_id": "f1", "name": "ABC-123"}],
        }

    async def get_folder_info(self, file_id: str) -> dict[str, Any]:
        return {"file_name": "ABC-123.mp4", "file_category": "1", "pick_code": "pf1"}

    async def walk_videos(self, root_id: str, *, pacer: Any = None) -> list[RemoteVideo]:
        return [_video("f1", "ABC-123.mp4")]


def test_poller_refuses_export_without_emby_mapping(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = _offline_db(tmp_path)
    pan = PanService(data_dir=data_dir)
    export_root = tmp_path / "emby"
    pan.save_settings(
        {
            "strm_enabled": True,
            "strm_output_root": str(export_root),
            "strm_mode": "relay",
            "strm_public_base_url": "http://nas:8700",
        }
    )
    pan.status = lambda: {"connected": True}  # type: ignore[method-assign]
    fake = _Fake115()
    pan.get_client = lambda: fake  # type: ignore[method-assign,assignment,return-value]
    store = MediaServerStore(data_dir / "ms.json")
    store.save(MediaServerSettings(enabled=True, kind="emby", base_url="http://emby", api_key="k"))

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = PanOfflinePoller(database=database, pan=pan, media_server_store=store, http=http)
            await poller.poll_once()
            assert not export_root.exists()
            assert poller.notifier.pending == {}
            with database.session() as session:
                task = Repository(session).list_pan_offline_tasks()[0]
            assert task.status == "done"
            assert task.error and "strm_emby_root" in task.error
            with pytest.raises(StrmConfigError):
                await poller.rewrite()
            assert poller.maintenance.last_config_error

    asyncio.run(scenario())


def test_settings_api_reports_config_errors_and_rewrite_400(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'api.sqlite'}")
    with TestClient(app) as client:
        response = client.put(
            "/api/pan/settings",
            json={"strm_enabled": True, "strm_mode": "relay", "strm_output_root": str(tmp_path / "out")},
        )
        assert response.status_code == 200
        errors = response.json()["strm_config_errors"]
        assert any("strm_public_base_url" in item for item in errors)
        assert client.get("/api/pan/settings").json()["strm_config_errors"] == errors
        rewrite = client.post("/api/pan/strm/rewrite")
        assert rewrite.status_code == 400
        assert "strm_public_base_url" in rewrite.json()["detail"]
        assert client.get("/api/pan/strm/status").json()["config_errors"] == errors
        ok = client.put(
            "/api/pan/settings",
            json={
                "strm_enabled": True,
                "strm_mode": "relay",
                "strm_output_root": str(tmp_path / "out"),
                "strm_public_base_url": "http://nas:8700",
            },
        )
        assert ok.json()["strm_config_errors"] == []


# ---------------------------------------------------------- 3. quota split


def test_relay_uses_sidecar_pick_code_so_only_downurl_hits_api(tmp_path: Path) -> None:
    root = tmp_path / "emby"
    export_work(settings=_relay(root), code="ABC-9", videos=[_video("77", "a.mp4")])
    calls: list[str] = []
    future = 1_900_000_000

    class _Client:
        async def get_folder_info(self, file_id: str) -> dict[str, Any]:
            calls.append(f"info:{file_id}")
            return {"pick_code": f"api-{file_id}"}

        async def download_url(self, pick_code: str, *, user_agent: str) -> str:
            calls.append(f"downurl:{pick_code}")
            return f"https://cdn.115/{pick_code}?t={future}"

    from shadow_mdc.services.pan import SourceFingerprint

    async def _read(factory):
        return await factory()

    pan = SimpleNamespace(
        status=lambda: {"connected": True},
        get_client=lambda: _Client(),
        config_store=SimpleNamespace(load=lambda: _relay(root)),
        source_fingerprint=lambda: SourceFingerprint(
            account_id="a", directory_id="d", auth_version=1, backend="115_open"
        ),
        read_after_check=_read,
    )
    relay = StrmRelay(pan)  # type: ignore[arg-type]
    target = asyncio.run(relay.resolve("77", "Emby"))
    assert target.url.startswith("https://cdn.115/pc77")
    assert calls == ["downurl:pc77"]
    # Unknown id (never exported) still falls back to the API lookup.
    asyncio.run(relay.resolve("88", "Emby"))
    assert calls[-2:] == ["info:88", "downurl:api-88"]


def test_artwork_fetch_bounded_retries_short_timeout(tmp_path: Path) -> None:
    attempts: list[float | None] = []
    statuses = [503, 429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        timeout = request.extensions.get("timeout", {}).get("read")
        attempts.append(timeout)
        status = statuses[len(attempts) - 1] if len(attempts) <= len(statuses) else 200
        if status != 200:
            return httpx.Response(status, headers={"Retry-After": "120"})
        return httpx.Response(200, content=b"\xff\xd8jpeg", headers={"content-type": "image/jpeg"})

    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    work = SimpleNamespace(id="w", artwork=[{"kind": "poster", "url": "https://img.example/p.jpg"}])

    async def run(retries: int) -> tuple[Any, dict[str, str]]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            store = ArtworkStore(
                tmp_path / f"art{retries}",
                http,
                max_bytes=1 << 20,
                timeout=7.0,
                retries=retries,
                sleep=fake_sleep,
            )
            return await store.acquire(work)  # type: ignore[arg-type]

    result, paths = asyncio.run(run(2))
    assert result.downloaded == 1 and paths
    assert attempts == [7.0, 7.0, 7.0]
    assert slept and max(slept) <= 5.0  # server Retry-After capped
    attempts.clear()
    statuses[:] = [404]
    result, _ = asyncio.run(run(3))
    assert result.failed == 1 and len(attempts) == 1  # permanent → no retry


# ------------------------------------------------------------- 4. drain


def _watch_service(tmp_path: Path, count: int) -> tuple[SubscriptionWatchService, list[str], Any]:
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    store = LibraryPrefsStore(tmp_path / "prefs.json")
    ids: list[str] = []
    with database.session() as session:
        repo = Repository(session)
        for index in range(count):
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id=f"drn{index}",
                    title=f"DRN-{index:03d}",
                    code=f"DRN-{index:03d}",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    source_url=f"https://javdb.com/v/drn{index}",
                ),
                overwrite=True,
            )
            ids.append(work.id)
    for work_id in ids:
        store.set_want(work_id, True)
    state = SubscriptionWatchStateStore(tmp_path / "state.json")
    pan = SimpleNamespace(
        config_store=SimpleNamespace(
            load=lambda: SimpleNamespace(subscription_auto_offline=True, offline_directory_id="d")
        ),
        status=lambda: {"connected": False},
    )
    service = SubscriptionWatchService(
        database=database,
        pan=pan,  # type: ignore[arg-type]
        discover=SimpleNamespace(),  # type: ignore[arg-type]
        library_prefs_store=store,
        state_store=state,
    )
    return service, sorted(ids), state


def test_watch_drains_whole_due_backlog_in_one_run(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    service, ordered, state = _watch_service(tmp_path, 60)
    monkeypatch.setattr(watch_module, "WATCH_PACE_SECONDS", 0.0)
    seen: list[str] = []

    async def record(work_id: str, *, stats: Any, pan_ready: bool) -> bool:
        seen.append(work_id)
        if len(seen) % 2:
            stats.hunting += 1
        else:
            stats.queued += 1
        return False

    monkeypatch.setattr(service, "_process_work", record)
    status = asyncio.run(service.run_once(limit=25))
    assert seen == ordered  # all 60 in one run, not one small batch
    assert status.last_due == 60 and status.batch_cursor == 60
    assert (status.last_hunting, status.last_queued) == (30, 30)
    seen.clear()
    asyncio.run(service.run_once(limit=25))
    assert seen == []
    # Pretend the last checks are old: they are due again.
    aged = (datetime.now(UTC) - timedelta(hours=1)).isoformat()
    current = state.load()
    state.save(current.model_copy(update={"checked_at": dict.fromkeys(current.checked_at, aged)}))
    asyncio.run(service.run_once(limit=25))
    assert seen == ordered


def test_due_work_ids_cutoff() -> None:
    cutoff = datetime(2026, 10, 4, 12, 0, tzinfo=UTC)
    checked = {
        "old": (cutoff - timedelta(hours=2)).isoformat(),
        "fresh": (cutoff - timedelta(minutes=1)).isoformat(),
        "after": (cutoff + timedelta(minutes=1)).isoformat(),
    }
    assert due_work_ids(["new", "old", "fresh", "after"], checked, cutoff) == ["new", "old"]


def test_watch_status_api_exposes_hunting_vs_queued(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'api.sqlite'}")
    with TestClient(app) as client:
        body = client.get("/api/pan/subscription-watch/status").json()
    assert {"last_hunting", "last_queued", "last_due", "draining"} <= set(body)
    assert "checked_at" not in body


# ------------------------------------------------------------ 5. pan auth


def test_single_flight_coalesces_and_does_not_cache_failures() -> None:
    flight: SingleFlight[int] = SingleFlight()
    calls = 0

    async def work() -> int:
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.01)
        return calls

    async def boom() -> int:
        raise RuntimeError("x")

    async def scenario() -> None:
        results = await asyncio.gather(*(flight.run("k", work) for _ in range(5)))
        assert results == [1] * 5
        with pytest.raises(RuntimeError):
            await flight.run("k", boom)
        assert await flight.run("k", work) == 2

    asyncio.run(scenario())


def _token_client(handler: Any) -> Pan115Client:
    client = Pan115Client(client_id="x")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return client


def test_115_concurrent_refresh_is_single_flight(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(pan_module, "RATE_LIMIT_SECONDS", 0.0)
    refreshes: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/open/refreshToken"):
            refreshes.append(request.content.decode())
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": {"access_token": "a2", "refresh_token": "r2", "expires_in": 7200},
                },
            )
        return httpx.Response(200, json={"state": True, "code": 0, "data": {}})

    client = _token_client(handler)
    saved: list[PanCredentials] = []
    client.bind_tokens(
        PanCredentials(access_token="a1", refresh_token="r1", expires_at="2000-01-01T00:00:00+00:00"),
        on_tokens=saved.append,
    )

    async def scenario() -> None:
        await asyncio.gather(*(client.fetch_user_info() for _ in range(4)))

    asyncio.run(scenario())
    assert len(refreshes) == 1 and "r1" in refreshes[0]
    assert client._access_token == "a2" and len(saved) == 1


def test_115_rejected_refresh_clears_credentials_and_needs_relogin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(pan_module, "RATE_LIMIT_SECONDS", 0.0)
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.path)
        if request.url.path.endswith("/open/refreshToken"):
            return httpx.Response(
                200, json={"state": 0, "code": 40140116, "message": "refresh_token invalid"}
            )
        return httpx.Response(401)

    pan = PanService(data_dir=tmp_path / "data")
    pan.import_tokens("a1", "r1", expires_in=7200)
    client = pan.get_client()
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))

    async def scenario() -> None:
        with pytest.raises(PanAuthRejectedError):
            await client.fetch_user_info()
        before = len(calls)
        with pytest.raises(pan_module.PanNotConfiguredError):
            await client.fetch_user_info()
        assert len(calls) == before  # no refresh loop
        await client.aclose()

    asyncio.run(scenario())
    assert pan.credentials_store.load() is None
    status = pan.status()
    assert status["connected"] is False and status["needs_relogin"] is True
    assert "re-login" in str(status["reason"])
    # A new login lifts the marker.
    pan.import_tokens("a3", "r3")
    assert pan.status()["needs_relogin"] is False


def test_115_late_rejection_cannot_clear_newer_login(tmp_path: Path) -> None:
    pan = PanService(data_dir=tmp_path / "data")
    pan.import_tokens("a-new", "r-new")
    pan._on_115_rejected("r-old", "late")
    assert pan.credentials_store.load() is not None
    assert pan.status()["needs_relogin"] is False


def test_openlist_concurrent_login_single_flight_and_token_rejection(tmp_path: Path) -> None:
    logins: list[int] = []
    valid = {"jwt-1"}
    static = {"tok": False}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/auth/login":
            logins.append(1)
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"token": "jwt-1"}})
        auth = request.headers.get("Authorization")
        if auth in valid or (auth == "tok" and static["tok"]):
            return httpx.Response(200, json={"code": 200, "message": "ok", "data": {"username": "admin"}})
        return httpx.Response(200, json={"code": 401, "message": "token is invalidated", "data": None})

    pan = PanService(data_dir=tmp_path / "data", openlist_transport=httpx.MockTransport(handler))
    pan.save_settings({"pan_backend": "openlist", "openlist_base_url": "http://ol:5244"})
    pan.set_openlist_credentials(username="admin", password="pw")

    async def concurrent() -> None:
        client = pan.openlist.client()
        await asyncio.gather(*(client.me() for _ in range(5)))

    asyncio.run(concurrent())
    assert len(logins) == 1

    # Static token only (no password) that the server rejects → cleared at once.
    pan.clear_openlist_credentials()
    pan.set_openlist_credentials(token="tok")
    calls_before = len(logins)

    async def rejected() -> None:
        client = pan.openlist.client()
        with pytest.raises(PanAuthRejectedError):
            await client.me()
        with pytest.raises(PanAuthRejectedError):
            await client.me()

    asyncio.run(rejected())
    assert pan.openlist_credentials.load().token is None
    status = pan.status()
    assert status["needs_relogin"] is True and status["connected"] is False
    assert pan.openlist.configured() is False
    assert len(logins) == calls_before
    pan.set_openlist_credentials(token="tok")
    static["tok"] = True
    assert pan.status()["needs_relogin"] is False

    async def works_again() -> None:
        assert (await pan.openlist.client().me())["username"] == "admin"
        await pan.aclose()

    asyncio.run(works_again())
