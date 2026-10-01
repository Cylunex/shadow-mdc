"""Peer-sync 2026-10-01 gaps (own implementation; ideas only):

* STRM export-root change → move managed folders + rewrite;
* process-wide Retry-After gate for 115 / OpenList;
* durable Emby Media/Updated queue, no Library/Refresh fallback;
* STRM maintenance off the offline-poll loop; offline submit outside a DB snapshot;
* actor portraits served locally (lazy localize), never raw CDN URLs.
"""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shadow_mdc.db.models import Actor, PanOfflineTask
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.actor_images import ActorImageCache, remote_key
from shadow_mdc.services.emby_notify import EmbyNotifier
from shadow_mdc.services.media_server import MediaServerConnector, MediaServerSettings, MediaServerStore
from shadow_mdc.services.openlist import OpenListClient, OpenListCredentials, OpenListCredentialStore
from shadow_mdc.services.pan import Pan115Client, PanService, PanSettings, RemoteVideo
from shadow_mdc.services.pan_common import BackoffGate, shared_gate
from shadow_mdc.services.pan_poller import PanOfflinePoller
from shadow_mdc.services.strm_export import SIDECAR_NAME, export_work, migrate_strm_tree

JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 4000


def _relay(root: Path, token: str = "tok") -> PanSettings:
    return PanSettings(
        strm_enabled=True,
        strm_output_root=str(root),
        strm_mode="relay",
        strm_public_base_url="http://nas:8700",
        strm_token=token,
        strm_emby_root="/media/strm",
    )


# ------------------------------------------------------------------ 1. migrate


def test_migrate_moves_managed_exports_and_rewrites(tmp_path: Path) -> None:
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    export_work(settings=_relay(old_root), code="ABC-123", videos=[RemoteVideo("v1", "ABC-123.mp4")])
    export_work(settings=_relay(old_root), code="XYZ-9", videos=[RemoteVideo("v9", "XYZ-9.mp4")])
    (old_root / "README.txt").write_text("not ours", encoding="utf-8")
    # Pre-existing export at the target for XYZ-9 must never be overwritten.
    (new_root / "XYZ-9").mkdir(parents=True)
    (new_root / "XYZ-9" / SIDECAR_NAME).write_text("{}", encoding="utf-8")

    settings = _relay(new_root, token="new")
    result = migrate_strm_tree(old_root, new_root, settings)

    assert [(s.name, t.name) for s, t in result.moved] == [("ABC-123", "ABC-123")]
    assert [p.name for p in result.conflicts] == ["XYZ-9"]
    assert not (old_root / "ABC-123").exists()
    assert (old_root / "XYZ-9" / "XYZ-9.strm").is_file()  # conflict left in place
    assert (old_root / "README.txt").is_file()  # unmanaged file untouched
    body = (new_root / "ABC-123" / "ABC-123.strm").read_text().strip()
    assert body == "http://nas:8700/api/strm/play/v1?token=new"
    assert (new_root / "ABC-123" / SIDECAR_NAME).is_file()


def test_migrate_into_nested_new_root(tmp_path: Path) -> None:
    old_root = tmp_path / "media"
    new_root = old_root / "strm"
    export_work(settings=_relay(old_root), code="ABC-1", videos=[RemoteVideo("v1", "ABC-1.mp4")])
    result = migrate_strm_tree(old_root, new_root, _relay(new_root))
    assert [t for _, t in result.moved] == [new_root / "ABC-1"]
    # Second run is a no-op (already under the new root).
    again = migrate_strm_tree(old_root, new_root, _relay(new_root))
    assert again.moved == [] and again.conflicts == []


def test_poller_migrate_rebases_rows_and_notifies(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    result = export_work(settings=_relay(old_root), code="ABC-123", videos=[RemoteVideo("v1", "ABC-123.mp4")])
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="abc",
                title="t",
                code="ABC-123",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                release_date=date(2026, 9, 1),
            ),
            overwrite=True,
        )
        row = repo.create_pan_offline_task(work_id=work.id, info_hash="H" * 40, directory_id="d")
        repo.update_pan_offline_task(row, status="done", strm_path=str(result.strm_paths[0]))

    pan = PanService(data_dir=data_dir)
    before = pan.save_settings(_relay(old_root).model_dump())
    pan.save_settings({"strm_output_root": str(new_root), "strm_emby_root": "/media/new"})

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = PanOfflinePoller(
                database=database,
                pan=pan,
                media_server_store=MediaServerStore(data_dir / "media-server.json"),
                http=http,
                data_dir=data_dir,
            )
            migrated = await poller.migrate_root(str(old_root), before)
            assert len(migrated.moved) == 1
            assert poller.notifier.pending == {
                "/media/strm/ABC-123": "Deleted",
                "/media/new/ABC-123": "Created",
            }
            assert poller.maintenance.last_migrate["moved"] == 1

    asyncio.run(scenario())
    with database.session() as session:
        stored = session.scalars(select(PanOfflineTask)).one()
        assert stored.strm_path == str(new_root / "ABC-123" / "ABC-123.strm")


def test_settings_api_triggers_migration_on_root_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")
    old_root = tmp_path / "old"
    new_root = tmp_path / "new"
    with TestClient(app_module().app) as client:
        poller = client.app.state.runtime.pan_poller  # type: ignore[attr-defined]
        calls: list[tuple[str | None, str | None]] = []

        async def fake_migrate(old: str | None, before: PanSettings | None = None) -> None:
            calls.append((old, before.strm_output_root if before else None))

        async def fake_rewrite() -> None:
            calls.append(("rewrite", None))

        monkeypatch.setattr(poller, "migrate_root", fake_migrate)
        monkeypatch.setattr(poller, "rewrite", fake_rewrite)
        assert client.put("/api/pan/settings", json={"strm_output_root": str(old_root)}).status_code == 200
        assert client.put("/api/pan/settings", json={"strm_output_root": str(new_root)}).status_code == 200
        assert client.put("/api/pan/settings", json={"strm_token": "x"}).status_code == 200
        status = client.get("/api/pan/strm/status").json()
        assert set(status["rate_limit"]) == {"115", "openlist"}
    assert calls == [(None, None), (str(old_root), str(old_root)), ("rewrite", None)]


def app_module() -> Any:
    import shadow_mdc.api as module

    return module


# --------------------------------------------------------- 2. shared backoff


def test_backoff_gate_extends_and_resets() -> None:
    now = [100.0]
    gate = BackoffGate("t", clock=lambda: now[0])
    assert gate.note(200, None) == 0.0
    assert gate.note(429, None) == 1.0
    assert gate.note(429, None) == 2.0  # exponential without Retry-After
    assert gate.remaining() == pytest.approx(2.0)
    assert gate.note(503, "30") == 30.0
    assert gate.remaining() == pytest.approx(30.0)
    gate.note(200, None)
    assert gate.note(429, None) == 1.0  # strikes reset after success, deadline kept
    assert gate.remaining() == pytest.approx(30.0)


def test_backoff_gate_rechecks_after_concurrent_extension() -> None:
    now = [0.0]
    gate = BackoffGate("t", clock=lambda: now[0])
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)
        if len(slept) == 1:
            gate.note(429, "5")  # another request got throttled meanwhile
        now[0] += seconds

    gate._sleep = fake_sleep  # type: ignore[assignment]
    gate.note(429, "2")
    asyncio.run(gate.wait())
    assert slept == [2.0, 3.0]  # re-waits until the extended deadline (t=5)


def test_115_retry_after_pauses_other_clients() -> None:
    """A 429 seen by one Pan115Client delays requests made through another."""

    seen: list[tuple[str, float]] = []
    loop_time: list[float] = []

    def handler(request: httpx.Request) -> httpx.Response:
        now = asyncio.get_event_loop().time()
        seen.append((request.url.path, now))
        if request.url.path == "/a" and sum(1 for p, _ in seen if p == "/a") == 1:
            return httpx.Response(429, headers={"Retry-After": "1"})
        return httpx.Response(200, json={})

    async def scenario() -> None:
        first = Pan115Client(client_id="x")
        second = Pan115Client(client_id="x")
        assert first.gate is second.gate is shared_gate("115")
        for client in (first, second):
            client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        loop_time.append(asyncio.get_event_loop().time())
        task_a = asyncio.create_task(first._request("GET", "https://proapi.115.com/a"))
        await asyncio.sleep(0.05)
        response_b = await second._request("GET", "https://proapi.115.com/b")
        response_a = await task_a
        assert response_a.status_code == 200 and response_b.status_code == 200
        await first.aclose()
        await second.aclose()

    asyncio.run(scenario())
    b_time = next(t for p, t in seen if p == "/b")
    assert b_time - loop_time[0] >= 0.9  # /b waited for the shared Retry-After window


def test_openlist_clients_share_gate(tmp_path: Path) -> None:
    store = OpenListCredentialStore(tmp_path / "ol.json")
    store.save(OpenListCredentials(token="t"))
    one = OpenListClient(base_url="http://ol:5244", credentials=store)
    two = OpenListClient(base_url="http://other:5244", credentials=store)
    assert one.gate is two.gate is shared_gate("openlist")
    assert one.gate is not shared_gate("115")


# ------------------------------------------------------------ 3. emby queue


def test_emby_queue_survives_restart_and_backs_off(tmp_path: Path) -> None:
    state = tmp_path / "queue.json"
    settings = MediaServerSettings(enabled=True, base_url="http://emby", api_key="k")
    wall = [1000.0]
    posts: list[list[dict[str, str]]] = []
    fail = [True]

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/Library/Media/Updated"
        posts.append(json.loads(request.content)["Updates"])
        return httpx.Response(500 if fail[0] else 204)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            first = EmbyNotifier(
                load_settings=lambda: settings, client=http, state_path=state, wall_clock=lambda: wall[0]
            )
            first.enqueue(["/m/A", "/m/B"], "Created")
            result = await first.flush()
            assert not result.ok
            details = first.pending_details()
            assert details["/m/A"].attempts == 1 and details["/m/A"].next_attempt == 1015.0
            assert (await first.flush()).detail == "nothing due"  # backing off

            # "Restart": a new notifier resumes from disk.
            second = EmbyNotifier(
                load_settings=lambda: settings, client=http, state_path=state, wall_clock=lambda: wall[0]
            )
            assert second.pending == {"/m/A": "Created", "/m/B": "Created"}
            assert second.retry_now() == 2
            fail[0] = False
            assert (await second.flush()).ok
            assert second.pending == {}
            third = EmbyNotifier(load_settings=lambda: settings, client=http, state_path=state)
            assert third.pending == {}

    asyncio.run(scenario())
    assert all("Library/Refresh" not in json.dumps(batch) for batch in posts)
    assert len(posts) == 2


def test_emby_queue_keeps_path_reenqueued_during_flush(tmp_path: Path) -> None:
    settings = MediaServerSettings(enabled=True, base_url="http://emby", api_key="k")
    holder: dict[str, EmbyNotifier] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        holder["n"].enqueue(["/m/A"], "Modified")  # newer revision while in flight
        return httpx.Response(204)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            notifier = EmbyNotifier(
                load_settings=lambda: settings, client=http, state_path=tmp_path / "q.json"
            )
            holder["n"] = notifier
            notifier.enqueue(["/m/A"], "Created")
            assert (await notifier.flush()).ok
            # Re-enqueued during the in-flight batch → kept for the next flush.
            assert notifier.pending == {"/m/A": "Created"}

    asyncio.run(scenario())


def test_refresh_path_never_falls_back_to_full_library_refresh() -> None:
    paths: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(500)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            connector = MediaServerConnector(
                settings=MediaServerSettings(enabled=True, base_url="http://emby", api_key="k"), client=http
            )
            result = await connector.refresh_path("/m/A")
            assert result.attempted and not result.ok

    asyncio.run(scenario())
    assert paths == ["/Library/Media/Updated"]


# ---------------------------------------------------- 4. worker isolation


def test_reconcile_runs_off_the_offline_poll_loop(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shadow_mdc.services.pan_poller as poller_module

    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    pan = PanService(data_dir=tmp_path / "data")
    monkeypatch.setattr(poller_module, "POLL_IDLE_SECONDS", 0.01)
    monkeypatch.setattr(poller_module, "POLL_ACTIVE_SECONDS", 0.01)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = PanOfflinePoller(
                database=database,
                pan=pan,
                media_server_store=MediaServerStore(tmp_path / "ms.json"),
                http=http,
            )
            polls = 0
            blocker = asyncio.Event()

            async def slow_reconcile() -> None:
                await blocker.wait()  # a very long delete walk

            async def poll_once() -> bool:
                nonlocal polls
                polls += 1
                return False

            monkeypatch.setattr(poller, "maybe_reconcile", slow_reconcile)
            monkeypatch.setattr(poller, "poll_once", poll_once)
            poller.start()
            await asyncio.sleep(0.2)
            await poller.stop()
            assert polls >= 3

    asyncio.run(scenario())


def test_offline_submit_happens_outside_db_transaction(tmp_path: Path) -> None:
    from shadow_mdc.services.pan_offline_enqueue import enqueue_work_offline

    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    with database.session() as session:
        work = Repository(session).upsert_provider_record(
            ProviderRecord(
                provider="javdb", external_id="e", title="t", code="ABC-1", family=ContentFamily.JAV
            ),
            overwrite=True,
        )
        work_id = work.id

    class FakePan:
        in_tx: bool | None = None

        def backend(self) -> str:
            return "115_open"

        def status(self) -> dict[str, object]:
            return {"connected": True}

        def offline_target(self) -> str:
            return "cid"

        async def submit_offline_url(self, url: str, **_kwargs: Any) -> dict[str, object]:
            FakePan.in_tx = session_ref.in_transaction()
            return {"info_hash": "A" * 40}

    async def scenario() -> None:
        global session_ref
        with database.session() as session:
            session_ref = session
            repo = Repository(session)
            repo.get_work(work_id)  # opens a read transaction
            result = await enqueue_work_offline(
                repo, FakePan(), work_id, url="magnet:?xt=urn:btih:" + "a" * 40  # type: ignore[arg-type]
            )
            assert result.created

    asyncio.run(scenario())
    assert FakePan.in_tx is False


session_ref: Any = None


# ----------------------------------------------------------- 5. actor images


def test_actor_image_display_url_and_lazy_localize(tmp_path: Path) -> None:
    cdn = "https://cdn.jsdelivr.net/gh/gfriends/x/a.jpg"
    requests: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        if request.url.path.endswith("bad.jpg"):
            return httpx.Response(200, content=b"<html>blocked</html>" * 100)
        return httpx.Response(200, content=JPEG)

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
            cache = ActorImageCache(tmp_path / "actor-images", http=http, url_source=lambda: [cdn])
            assert cache.display_url(None) is None
            assert cache.display_url("/api/actor-images/x.jpg") == "/api/actor-images/x.jpg"
            key = remote_key(cdn)
            assert cache.display_url(cdn) == f"/api/actor-images/remote/{key}"
            # Fresh instance (restart): key resolved via the catalog URL source.
            fresh = ActorImageCache(tmp_path / "actor-images", http=http, url_source=lambda: [cdn])
            path = await fresh.localize(key)
            assert path is not None and path.read_bytes() == JPEG
            assert fresh.display_url(cdn) == f"/api/actor-images/{path.name}"
            assert await fresh.localize(key) == path
            assert len(requests) == 1
            # Unknown key → never fetched (not an open proxy).
            assert await fresh.localize("0" * 32) is None
            bad = "https://img.example/bad.jpg"
            fresh.display_url(bad)
            assert await fresh.localize(remote_key(bad)) is None  # not image bytes
            assert await fresh.localize(remote_key(bad)) is None  # negative-cached
            assert len(requests) == 2

    asyncio.run(scenario())


def test_actor_apis_never_return_cdn_urls(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from shadow_mdc.services.actor_images import configure_actor_images

    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'actors.db'}")
    cdn = "https://pics.dmm.co.jp/mono/actjpgs/someone.jpg"
    with TestClient(app_module().app) as client:
        database = client.app.state.runtime.database  # type: ignore[attr-defined]
        with database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="e1",
                    title="t",
                    code="ABC-777",
                    family=ContentFamily.JAV,
                    actors=("演员丙",),
                ),
                overwrite=True,
            )
            work_id = work.id
            actor = session.scalars(select(Actor)).one()
            actor.image_url = cdn
        transport = httpx.MockTransport(lambda r: httpx.Response(200, content=JPEG))
        http = httpx.AsyncClient(transport=transport)
        configure_actor_images(
            ActorImageCache(data_dir / "actor-images", http=http, url_source=lambda: [cdn])
        )
        detail = client.get(f"/api/works/{work_id}").json()
        actors = client.get("/api/actors").json()
        urls = [item["image_url"] for item in detail["actor_entities"]] + [a["image_url"] for a in actors]
        assert urls and all(url and not url.startswith("http") for url in urls)
        lazy = next(url for url in urls if "/remote/" in url)
        image = client.get(lazy)
        assert image.status_code == 200 and image.content == JPEG
        # After first view the list points straight at the local file.
        again = client.get(f"/api/works/{work_id}").json()["actor_entities"][0]["image_url"]
        assert again.startswith("/api/actor-images/remote-")
        assert client.get(again).content == JPEG
        assert client.get("/api/actor-images/remote/" + "f" * 32).status_code == 404
