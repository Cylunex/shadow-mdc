"""OpenList backend: auth, offline submit/dedup, polling → STRM, relay, reconcile, settings API.

All OpenList traffic is served by an in-process fake (httpx.MockTransport).
"""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import date
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services import pan_poller as poller_module
from shadow_mdc.services.media_server import MediaServerStore
from shadow_mdc.services.openlist import (
    OpenListAuthError,
    build_openlist_d_url,
    code_pattern,
    map_openlist_state,
    normalize_openlist_path,
    parse_task,
)
from shadow_mdc.services.pan import PanOfflineConflictError, PanService, ScanPacer
from shadow_mdc.services.pan_offline_enqueue import enqueue_work_offline
from shadow_mdc.services.pan_poller import PanOfflinePoller
from shadow_mdc.services.strm_export import read_sidecar

BASE = "http://openlist.lan:5244"
TARGET = "/115/云下载"
HASH = "A" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=ABC-123"
TOKEN = "secret-api-token-xyz"
PASSWORD = "hunter2-very-secret"


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": 200, "message": "success", "data": data})


def _err(code: int, message: str) -> httpx.Response:
    return httpx.Response(200, json={"code": code, "message": message, "data": None})


class FakeOpenList:
    """Minimal OpenList API: auth, fs/list, fs/get, add_offline_download, task lists."""

    def __init__(self) -> None:
        self.valid_tokens = {TOKEN}
        self.users = {"admin": PASSWORD}
        self.calls: list[tuple[str, str, dict[str, Any] | None, str | None]] = []
        self.undone: list[dict[str, Any]] = []
        self.done: list[dict[str, Any]] = []
        self.transfers: list[dict[str, Any]] = []
        self.add_error: str | None = None
        self.next_tid = 1
        self.sign_enabled = False
        # path -> list of entries
        self.tree: dict[str, list[dict[str, Any]]] = {TARGET: []}
        self.storage_missing = False

    def task_name(self, url: str, dst: str = TARGET) -> str:
        return f"download {url} to ({dst})"

    def add_result(self, folder: str, files: list[tuple[str, int]]) -> None:
        self.tree[TARGET].append(
            {"name": folder, "is_dir": True, "size": 0, "modified": "2026-10-01T10:00:00.123456789+08:00"}
        )
        self.tree[f"{TARGET}/{folder}"] = [
            {"name": name, "is_dir": False, "size": size, "modified": "2026-10-01T10:05:00Z"}
            for name, size in files
        ]

    def _obj(self, item: dict[str, Any], path: str) -> dict[str, Any]:
        out = dict(item)
        out["sign"] = f"sig:{path}" if self.sign_enabled and not item.get("is_dir") else ""
        return out

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body: dict[str, Any] | None = None
        if request.content:
            body = json.loads(request.content)
        auth = request.headers.get("Authorization")
        self.calls.append((request.method, request.url.path, body, auth))
        path = request.url.path
        if path == "/api/auth/login":
            assert body is not None
            if self.users.get(str(body.get("username"))) == body.get("password"):
                token = f"jwt-{len(self.valid_tokens)}"
                self.valid_tokens.add(token)
                return _ok({"token": token})
            return _err(400, "password is incorrect")
        if path == "/api/public/offline_download_tools":
            return _ok(["SimpleHttp", "115 Cloud", "115 Open"])
        if auth not in self.valid_tokens:
            return _err(401, "token is invalidated")
        if path == "/api/me":
            return _ok({"username": "admin", "base_path": "/", "permission": 0xFFFF})
        if path == "/api/fs/list":
            assert body is not None
            entries = self.tree.get(body["path"])
            if entries is None:
                return _err(500, "failed get objs: object not found")
            per_page = int(body.get("per_page") or 200)
            page = int(body.get("page") or 1)
            chunk = entries[(page - 1) * per_page : page * per_page]
            base = body["path"].rstrip("/")
            return _ok(
                {
                    "content": [self._obj(item, f"{base}/{item['name']}") for item in chunk] or None,
                    "total": len(entries),
                    "write": True,
                    "provider": "115 Cloud",
                }
            )
        if path == "/api/fs/get":
            assert body is not None
            target = body["path"]
            if self.storage_missing:
                return _err(500, "failed get storage: storage not found; please add a storage first")
            parent, _, name = target.rpartition("/")
            for item in self.tree.get(parent or "/", []):
                if item["name"] == name:
                    return _ok({**self._obj(item, target), "raw_url": f"https://cdn.example/{name}"})
            return _err(500, "failed get obj: object not found")
        if path == "/api/fs/add_offline_download":
            assert body is not None
            if self.add_error:
                return _err(500, self.add_error)
            tid = f"tid-{self.next_tid}"
            self.next_tid += 1
            task = {
                "id": tid,
                "name": self.task_name(body["urls"][0], body["path"]),
                "state": 0,
                "status": "",
                "progress": 0,
                "error": "",
            }
            self.undone.append(task)
            return _ok({"tasks": [task]})
        if path == "/api/task/offline_download/undone":
            return _ok(self.undone)
        if path == "/api/task/offline_download/done":
            return _ok(self.done)
        if path == "/api/task/offline_download_transfer/undone":
            return _ok(self.transfers)
        return httpx.Response(404, text="not found")

    def finish(self, tid: str, *, state: int = 2, error: str = "") -> None:
        for task in list(self.undone):
            if task["id"] == tid:
                self.undone.remove(task)
                self.done.append({**task, "state": state, "progress": 100, "error": error})


def _pan(tmp_path: Path, fake: FakeOpenList, **settings: Any) -> PanService:
    pan = PanService(data_dir=tmp_path / "data", openlist_transport=httpx.MockTransport(fake))
    pan.save_settings(
        {
            "pan_backend": "openlist",
            "openlist_base_url": BASE + "/",
            "openlist_offline_path": "115/云下载/",
            **settings,
        }
    )
    pan.set_openlist_credentials(token=TOKEN)
    return pan


def _no_pacing(monkeypatch: pytest.MonkeyPatch) -> None:
    async def no_sleep(_seconds: float) -> None:
        return None

    original_init = ScanPacer.__init__

    def init(self: ScanPacer, *args: Any, **kwargs: Any) -> None:
        kwargs["sleep"] = no_sleep
        original_init(self, *args, **kwargs)

    monkeypatch.setattr(ScanPacer, "__init__", init)
    monkeypatch.setattr("shadow_mdc.services.openlist.OPENLIST_MIN_INTERVAL", 0.0)


def _database(tmp_path: Path) -> tuple[Database, str]:
    database = Database(f"sqlite:///{tmp_path / 'db.sqlite'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="abc123",
                title="ABC-123 title",
                code="ABC-123",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url="https://javdb.com/v/abc123",
                release_date=date(2026, 9, 1),
            ),
            overwrite=True,
        )
        repo.save_work_magnets(
            work,
            [{"uri": MAGNET, "info_hash": HASH, "name": "ABC-123", "hd": True}],
            provider="javdb",
        )
        return database, work.id


# --------------------------------------------------------------------- helpers


def test_helpers_paths_states_and_code_pattern() -> None:
    assert normalize_openlist_path("115/云下载/") == "/115/云下载"
    assert normalize_openlist_path("  ") is None
    with pytest.raises(ValueError):
        normalize_openlist_path("/115/../etc")
    assert (
        build_openlist_d_url(BASE, "/115/云下载/[X] a b.mp4", "s=1")
        == f"{BASE}/d/115/%E4%BA%91%E4%B8%8B%E8%BD%BD/%5BX%5D%20a%20b.mp4?sign=s%3D1"
    )
    assert [map_openlist_state(value) for value in (0, 1, 2, 4, 5, 7, 8, None)] == [
        "running",
        "running",
        "done",
        "failed",
        "running",
        "failed",
        "running",
        "running",
    ]
    pattern = code_pattern("SONE-118")
    assert pattern is not None
    assert pattern.search("[HD] sone118-C.mp4")
    assert pattern.search("SONE_00118")
    assert not pattern.search("SONE-1180")
    assert not pattern.search("XSONE-118")
    task = parse_task({"id": "t", "name": f"download {MAGNET} to ({TARGET})", "state": 1, "progress": 42.5})
    assert task.info_hash == HASH and task.dst == TARGET and task.local_status == "running"


# ------------------------------------------------------------------------ auth


def test_password_login_caches_session_and_relogs_on_401(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    fake = FakeOpenList()
    pan = PanService(data_dir=tmp_path / "data", openlist_transport=httpx.MockTransport(fake))
    pan.save_settings({"pan_backend": "openlist", "openlist_base_url": BASE})
    pan.set_openlist_credentials(username="admin", password=PASSWORD)
    caplog.set_level(logging.DEBUG)

    async def scenario() -> None:
        client = pan.openlist.client()
        me = await client.me()
        assert me["username"] == "admin"
        logins = [call for call in fake.calls if call[1] == "/api/auth/login"]
        assert len(logins) == 1
        # Session reused.
        await client.me()
        assert len([call for call in fake.calls if call[1] == "/api/auth/login"]) == 1
        # Server-side invalidation → one re-login, then success.
        fake.valid_tokens = {TOKEN}
        await client.me()
        assert len([call for call in fake.calls if call[1] == "/api/auth/login"]) == 2
        # Raw token header (no "Bearer").
        assert fake.calls[-1][3] and not fake.calls[-1][3].startswith("Bearer")
        # Wrong password surfaces an auth error.
        fake.users = {"admin": "other"}
        fake.valid_tokens = {TOKEN}
        with pytest.raises(OpenListAuthError):
            await client.me()
        await pan.aclose()

    asyncio.run(scenario())
    status = pan.settings_status()
    assert status["openlist_password_set"] is True
    assert status["openlist_token_set"] is False
    dumped = json.dumps(status) + json.dumps(pan.status()) + json.dumps(pan.account())
    assert PASSWORD not in dumped and "jwt-" not in dumped
    assert PASSWORD not in caplog.text and "jwt-" not in caplog.text
    secret_file = tmp_path / "data" / "pan" / "openlist-credentials.json"
    assert secret_file.stat().st_mode & 0o077 == 0


# ---------------------------------------------------------------- offline flow


def test_enqueue_submits_with_tool_and_dedups(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pacing(monkeypatch)
    fake = FakeOpenList()
    pan = _pan(tmp_path, fake, openlist_offline_tool="115 Open")
    database, work_id = _database(tmp_path)

    async def scenario() -> None:
        with database.session() as session:
            repo = Repository(session)
            magnet_id = repo.list_work_magnets(work_id)[0].id
            result = await enqueue_work_offline(repo, pan, work_id, magnet_id=magnet_id)
            assert result.created is True
            assert result.task.backend == "openlist"
            assert result.task.remote_task_id == "tid-1"
            assert result.task.directory_id == TARGET
            assert result.task.info_hash == HASH
            # Second call while running reuses the local row (no new OpenList call).
            again = await enqueue_work_offline(repo, pan, work_id, magnet_id=magnet_id)
            assert again.reused_running is True
        submits = [call for call in fake.calls if call[1] == "/api/fs/add_offline_download"]
        assert len(submits) == 1
        assert submits[0][2] == {
            "urls": [MAGNET],
            "path": TARGET,
            "tool": "115 Open",
            "delete_policy": "delete_on_upload_succeed",
        }
        assert submits[0][3] == TOKEN
        # A matching undone task on OpenList is adopted instead of re-submitted.
        adopted = await pan.submit_offline_url(MAGNET, directory_id=TARGET, work_code="ABC-123")
        assert adopted["adopted"] is True and adopted["remote_task_id"] == "tid-1"
        assert len([call for call in fake.calls if call[1] == "/api/fs/add_offline_download"]) == 1
        await pan.aclose()

    asyncio.run(scenario())


def test_exists_error_adopts_result_or_conflicts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pacing(monkeypatch)
    fake = FakeOpenList()
    fake.add_error = "failed to add offline download task: 任务已存在"
    pan = _pan(tmp_path, fake)

    async def scenario() -> None:
        with pytest.raises(PanOfflineConflictError):
            await pan.submit_offline_url(MAGNET, directory_id=TARGET, work_code="ABC-123")
        fake.add_result("ABC-123", [("ABC-123.mp4", 5_000)])
        result = await pan.submit_offline_url(MAGNET, directory_id=TARGET, work_code="ABC-123")
        assert result["adopted"] is True and result["remote_task_id"] is None
        await pan.aclose()

    asyncio.run(scenario())


def _poller(database: Database, pan: PanService, data_dir: Path, http: httpx.AsyncClient) -> PanOfflinePoller:
    return PanOfflinePoller(
        database=database,
        pan=pan,
        media_server_store=MediaServerStore(data_dir / "media-server.json"),
        http=http,
        data_dir=data_dir,
    )


def test_poll_progress_completion_export_rewrite_reconcile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _no_pacing(monkeypatch)
    fake = FakeOpenList()
    export_root = tmp_path / "emby"
    pan = _pan(
        tmp_path,
        fake,
        strm_enabled=True,
        strm_output_root=str(export_root),
        strm_emby_root="/media/strm",
        openlist_strm_base_url="http://192.168.0.5:5244",
    )
    database, work_id = _database(tmp_path)
    data_dir = tmp_path / "data"

    async def scenario() -> None:
        with database.session() as session:
            repo = Repository(session)
            magnet_id = repo.list_work_magnets(work_id)[0].id
            await enqueue_work_offline(repo, pan, work_id, magnet_id=magnet_id)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = _poller(database, pan, data_dir, http)
            fake.undone[0].update({"state": 1, "progress": 37.5})
            assert await poller.poll_once() is True
            with database.session() as session:
                row = Repository(session).list_pan_offline_tasks()[0]
                assert row.status == "running" and row.progress == 37.5

            # Download finished; the 115 result folder appears under the target.
            fake.finish("tid-1")
            fake.add_result(
                "[HD] ABC-123",
                [("ABC-123-CD2.mp4", 900), ("ABC-123-CD1.mp4", 1000), ("sample.mp4", 10)],
            )
            fake.tree[TARGET].append({"name": "OTHER-999", "is_dir": True, "size": 0})
            assert await poller.poll_once() is True
            folder = export_root / "ABC-123"
            cd1 = (folder / "ABC-123-cd1.strm").read_text().strip()
            cd2 = (folder / "ABC-123-cd2.strm").read_text().strip()
            assert cd1 == (
                "http://192.168.0.5:5244/d/115/%E4%BA%91%E4%B8%8B%E8%BD%BD/%5BHD%5D%20ABC-123/ABC-123-CD1.mp4"
            )
            assert cd2.endswith("/ABC-123-CD2.mp4")
            assert not (folder / "ABC-123-cd3.strm").exists()
            assert (folder / "ABC-123.nfo").is_file()
            entries = read_sidecar(folder)
            assert {entry.file_id for entry in entries} == {
                f"{TARGET}/[HD] ABC-123/ABC-123-CD1.mp4",
                f"{TARGET}/[HD] ABC-123/ABC-123-CD2.mp4",
            }
            assert poller.notifier.pending == {"/media/strm/ABC-123": "Created"}
            with database.session() as session:
                row = Repository(session).list_pan_offline_tasks()[0]
                assert row.status == "done"
                assert row.remote_path == f"{TARGET}/[HD] ABC-123"
                assert row.strm_path and row.strm_path.endswith("ABC-123-cd1.strm")

            # Relay mode: rewrite points at our /api/strm/openlist relay (keyed by path).
            pan.save_settings(
                {"strm_mode": "relay", "strm_public_base_url": "http://nas:8700", "strm_token": "tok"}
            )
            rewritten = await poller.rewrite()
            assert len(rewritten.rewritten) == 2
            assert (folder / "ABC-123-cd1.strm").read_text().strip() == (
                "http://nas:8700/api/strm/openlist/115/%E4%BA%91%E4%B8%8B%E8%BD%BD/"
                "%5BHD%5D%20ABC-123/ABC-123-CD1.mp4?token=tok"
            )

            # Storage unmounted → unknown, nothing deleted.
            fake.storage_missing = True
            result = await poller.reconcile()
            assert result.unknown == 1 and folder.exists()
            # Source deleted on 115 (OpenList says object not found) → folder removed.
            fake.storage_missing = False
            fake.tree[f"{TARGET}/[HD] ABC-123"] = []
            fake.tree[TARGET] = [item for item in fake.tree[TARGET] if item["name"] != "[HD] ABC-123"]
            result = await poller.reconcile()
            assert [path.name for path in result.removed] == ["ABC-123"]
            assert not folder.exists()
            assert poller.notifier.pending["/media/strm/ABC-123"] == "Deleted"
        await pan.aclose()

    asyncio.run(scenario())


def test_signed_export_and_failed_task(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pacing(monkeypatch)
    fake = FakeOpenList()
    fake.sign_enabled = True
    export_root = tmp_path / "emby"
    pan = _pan(tmp_path, fake, strm_enabled=True, strm_output_root=str(export_root), openlist_strm_sign=True)
    database, work_id = _database(tmp_path)
    data_dir = tmp_path / "data"

    async def scenario() -> None:
        with database.session() as session:
            repo = Repository(session)
            await enqueue_work_offline(repo, pan, work_id, url="magnet:?xt=urn:btih:" + "B" * 40)
            await enqueue_work_offline(repo, pan, work_id, magnet_id=repo.list_work_magnets(work_id)[0].id)
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = _poller(database, pan, data_dir, http)
            fake.finish("tid-1", state=7, error="115: torrent invalid")
            fake.finish("tid-2")
            fake.add_result("ABC-123", [("ABC-123.mkv", 1000)])
            await poller.poll_once()
            body = (export_root / "ABC-123" / "ABC-123.strm").read_text().strip()
            assert body == (
                f"{BASE}/d/115/%E4%BA%91%E4%B8%8B%E8%BD%BD/ABC-123/ABC-123.mkv"
                "?sign=sig%3A%2F115%2F%E4%BA%91%E4%B8%8B%E8%BD%BD%2FABC-123%2FABC-123.mkv"
            )
            with database.session() as session:
                rows = {row.info_hash: row for row in Repository(session).list_pan_offline_tasks()}
                assert rows["B" * 40].status == "failed"
                assert "torrent invalid" in (rows["B" * 40].error or "")
                assert rows[HASH].status == "done"
        await pan.aclose()

    asyncio.run(scenario())


def test_115_open_rows_untouched_in_openlist_mode(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _no_pacing(monkeypatch)
    fake = FakeOpenList()
    pan = _pan(tmp_path, fake)
    database, work_id = _database(tmp_path)
    with database.session() as session:
        Repository(session).create_pan_offline_task(work_id=work_id, info_hash="C" * 40, directory_id="cid")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(204))) as http:
            poller = _poller(database, pan, tmp_path / "data", http)
            assert await poller.poll_once() is False
        assert not [call for call in fake.calls if call[1].startswith("/api/task")]
        await pan.aclose()

    asyncio.run(scenario())
    assert poller_module.OPENLIST_MISSING_LIMIT > 0


# ------------------------------------------------------------------------- API


def _boot(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}")
    monkeypatch.setenv("SHADOW_MDC_TRANSLATION_ENABLED", "false")
    monkeypatch.setenv("SHADOW_MDC_AUTO_SEED_NON_JAV_WORKS", "false")
    return TestClient(app)


def test_settings_credentials_test_and_relay_endpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    fake = FakeOpenList()
    fake.add_result("ABC-123", [("ABC-123.mp4", 100)])
    with _boot(tmp_path, monkeypatch) as client:
        app.state.runtime.pan_service.openlist.set_transport(httpx.MockTransport(fake))
        initial = client.get("/api/pan/settings").json()
        assert initial["pan_backend"] == "115_open"
        assert initial["openlist_offline_tool"] == "115 Cloud"

        bad = client.put("/api/pan/settings", json={"openlist_base_url": "openlist:5244"})
        assert bad.status_code == 400
        saved = client.put(
            "/api/pan/settings",
            json={
                "pan_backend": "openlist",
                "openlist_base_url": BASE,
                "openlist_offline_path": TARGET,
                "strm_token": "tok",
            },
        )
        assert saved.status_code == 200, saved.text
        assert saved.json()["pan_backend"] == "openlist"
        status = client.get("/api/pan/status").json()
        assert status["provider"] == "openlist" and status["connected"] is False

        creds = client.post("/api/pan/openlist/credentials", json={"token": TOKEN})
        assert creds.json() == {
            "openlist_username": None,
            "openlist_token_set": True,
            "openlist_password_set": False,
        }
        settings_text = client.get("/api/pan/settings").text
        assert TOKEN not in settings_text
        assert client.get("/api/pan/status").json()["offline_ready"] is True

        test = client.post("/api/pan/openlist/test").json()
        assert test["ok"] is True
        assert test["user"] == "admin"
        assert test["target_ok"] is True and test["target_entries"] == 1
        assert test["tool_available"] is True
        assert TOKEN not in json.dumps(test)

        files = client.get("/api/pan/openlist/files", params={"path": TARGET}).json()
        assert files["items"][0] == {
            "name": "ABC-123",
            "path": f"{TARGET}/ABC-123",
            "is_directory": True,
            "size": 0,
        }

        relay = client.get(
            "/api/strm/openlist/115/云下载/ABC-123/ABC-123.mp4",
            params={"token": "tok"},
            follow_redirects=False,
        )
        assert relay.status_code == 302
        assert relay.headers["location"] == (
            f"{BASE}/d/115/%E4%BA%91%E4%B8%8B%E8%BD%BD/ABC-123/ABC-123.mp4"
        )
        assert client.get(
            "/api/strm/openlist/115/云下载/ABC-123/ABC-123.mp4", follow_redirects=False
        ).status_code == 403
        assert client.get(
            "/api/strm/openlist/other/x.mp4", params={"token": "tok"}, follow_redirects=False
        ).status_code == 404

        # Wrong token → test reports failure without leaking secrets.
        client.post("/api/pan/openlist/credentials", json={"token": "nope"})
        failed = client.post("/api/pan/openlist/test").json()
        assert failed["ok"] is False and "nope" not in json.dumps(failed)

        assert client.delete("/api/pan/openlist/credentials").status_code == 204
        assert client.get("/api/pan/settings").json()["openlist_token_set"] is False

        # Switching back keeps the 115 Open path intact.
        back = client.put("/api/pan/settings", json={"pan_backend": "115_open"})
        assert back.json()["pan_backend"] == "115_open"
        assert client.get("/api/pan/status").json()["provider"] == "115"
