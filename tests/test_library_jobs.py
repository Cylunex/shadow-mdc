"""Exercise durable task admission, recovery, retry and scheduled scan inputs."""

import hashlib
import time
from datetime import timedelta
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.api_models import ScanRequest
from shadow_mdc.db.models import Library, LibraryScanSchedule, utc_now
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints
from shadow_mdc.providers.base import SearchBatch
from shadow_mdc.services.library_jobs import CHECKPOINT_PREFIX, LibraryJobState, due_scans
from shadow_mdc.services.scanner import Scanner


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> str:
    url = f"sqlite:///{tmp_path / 'jobs.db'}"
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", url)
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path / "app-data"))
    return url


def _wait_task(client: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        result = client.get(f"/api/tasks/{task_id}").json()
        if result["finished_at"]:
            return result
        time.sleep(0.025)
    pytest.fail(f"task {task_id} did not finish")


def test_queue_deduplicates_and_cancel_has_a_terminal_state(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(tmp_path, monkeypatch)
    root = tmp_path / "library"
    root.mkdir()
    (root / "ABP-123.strm").write_text("https://example.com/example.mp4")
    started = Event()
    release = Event()
    original = Scanner._scan_asset

    def pause(
        self: Scanner, library: Library, base: Path, path: Path, *, volume_code: str | None = None,
    ) -> tuple[bool, bool, tuple[str, ...]]:
        started.set()
        assert release.wait(5)
        return original(self, library, base, path, volume_code=volume_code)

    monkeypatch.setattr(Scanner, "_scan_asset", pause)
    with TestClient(app) as client:
        library_id = client.post("/api/libraries", json={"name": "Test", "root_path": str(root)}).json()["id"]
        path = f"/api/libraries/{library_id}"
        try:
            first = client.post(path + "/scan/jobs", json={})
            assert first.status_code == 202
            task_id = first.json()["id"]
            assert started.wait(2)
            assert client.post(path + "/scan/jobs", json={}).json()["id"] == task_id
            assert client.post(path + "/identify/jobs", json={}).status_code == 409
            assert client.post(f"/api/tasks/{task_id}/cancel").status_code == 200
        finally:
            release.set()
        assert _wait_task(client, task_id)["status"] == "cancelled"
        retry = client.post(f"/api/tasks/{task_id}/retry")
        assert retry.status_code == 200
        assert _wait_task(client, retry.json()["id"])["status"] == "succeeded"


def test_startup_recovers_scan_without_reprocessing_a_committed_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    db = Database(_env(tmp_path, monkeypatch))
    db.initialize()
    root = tmp_path / "library"
    root.mkdir()
    done = root / "ABP-101.strm"
    pending = root / "ABP-102.strm"
    for path in (done, pending):
        path.write_text("https://example.com/" + path.stem + ".mp4")
    with db.session() as session:
        library = Library(name="Recovery", root_path=str(root))
        session.add(library)
        session.flush()
        repo = Repository(session)
        task = repo.create_task_run(kind="scan", scope=str(root))
        task_id = task.id
        Scanner(repo).scan(library)
        state = LibraryJobState(library_id=library.id, operation="scan", scan=ScanRequest())
        repo.save_checkpoint(
            CHECKPOINT_PREFIX + task_id, kind="scan", status="running",
            cursor=state.model_dump(mode="json"), task_id=task_id,
        )
        repo.save_library_scan_item(task_id, hashlib.sha256(str(done).encode()).hexdigest(), ())
    with TestClient(app) as client:
        result = _wait_task(client, task_id)
        assert result["status"] == "succeeded"
        assert result["summary"]["skipped"] == 1
        assert result["summary"]["updated"] == 1
    with db.session() as session:
        assert Repository(session).library_scan_items(task_id) == []


def test_identify_retries_only_failed_assets_and_schedule_is_opt_in(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(tmp_path, monkeypatch)
    root = tmp_path / "library"
    root.mkdir()
    for code in ("ABP-111", "ABP-112"):
        (root / (code + ".strm")).write_text("https://example.com/" + code + ".mp4")
    with TestClient(app) as client:
        library_id = client.post("/api/libraries", json={"name": "Test", "root_path": str(root)}).json()["id"]
        path = f"/api/libraries/{library_id}"
        assert client.post(path + "/scan", json={}).status_code == 200
        calls: list[str | None] = []

        async def search(hints: IdentityHints) -> SearchBatch:
            calls.append(hints.code)
            return SearchBatch(records=(), failures=())

        runtime = app.state.runtime
        monkeypatch.setattr(runtime.providers, "search", search)
        queued = client.post(path + "/identify/jobs", json={"limit": 1}).json()
        assert _wait_task(client, queued["id"])["status"] == "partial"
        assert len(calls) == 1
        retry = client.post(f"/api/tasks/{queued['id']}/retry").json()
        assert _wait_task(client, retry["id"])["status"] == "partial"
        assert calls == [calls[0], calls[0]]
        assert client.get(path + "/scan/schedule").json() == {"interval_hours": 0}
        assert client.put(path + "/scan/schedule", json={"interval_hours": 169}).status_code == 422
        assert client.put(path + "/scan/schedule", json={"interval_hours": 1}).status_code == 200
        with runtime.database.session() as session:
            assert session.get(LibraryScanSchedule, library_id) is not None
            assert due_scans(session, utc_now()) == []
            assert due_scans(session, utc_now() + timedelta(hours=2)) == [library_id]
        assert client.put(path + "/scan/schedule", json={"interval_hours": 0}).status_code == 200


def test_identify_does_not_share_same_title_results_across_different_cast(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _env(tmp_path, monkeypatch)
    root = tmp_path / "library"
    root.mkdir()
    for name in ("Performer A", "Performer B"):
        folder = root / name
        folder.mkdir()
        (folder / "Shared title.strm").write_text("https://example.com/shared.mp4")
    with TestClient(app) as client:
        library_id = client.post("/api/libraries", json={"name": "Test", "root_path": str(root)}).json()["id"]
        path = f"/api/libraries/{library_id}"
        assert client.post(path + "/scan", json={}).status_code == 200
        runtime = app.state.runtime
        with runtime.database.session() as session:
            repo = Repository(session)
            for asset in repo.list_library_assets(library_id):
                actor = Path(asset.path).parent.name
                hints = IdentityHints.model_validate(asset.hints).model_copy(update={"actors": (actor,)})
                repo.update_asset_hints(asset, hints)
        searched: list[tuple[str, ...]] = []

        async def search(hints: IdentityHints) -> SearchBatch:
            searched.append(hints.actors)
            return SearchBatch(records=(), failures=())

        monkeypatch.setattr(runtime.providers, "search", search)
        queued = client.post(path + "/identify/jobs", json={}).json()
        assert _wait_task(client, queued["id"])["status"] == "succeeded"
        assert set(searched) == {("Performer A",), ("Performer B",)}
