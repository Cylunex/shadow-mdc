"""Exercise the skill's real intake flow using temporary media and fake remote services."""

import time
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.media.magnets import MagnetLink, info_hash_from_uri
from shadow_mdc.providers.base import SearchBatch
from shadow_mdc.services.discover import DiscoverSeedResult

HASH = "B" * 40
MAGNET = f"magnet:?xt=urn:btih:{HASH}&dn=SSIS-123"


def record() -> ProviderRecord:
    return ProviderRecord(
        provider="javdb",
        external_id="fixture",
        title="Fixture title",
        code="SSIS-123",
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        actors=("Fixture actor",),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'skill.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST_TOKEN")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as test_client:
        runtime = app.state.runtime
        monkeypatch.setattr(
            runtime.providers, "search", AsyncMock(return_value=SearchBatch(records=(record(),), failures=()))
        )
        monkeypatch.setattr(runtime.discover, "r18_record", lambda code: None)
        monkeypatch.setattr(
            runtime.discover,
            "list_magnets",
            AsyncMock(
                return_value=(MagnetLink(info_hash=HASH, uri=MAGNET, name="SSIS-123", provider="javdb"),)
            ),
        )

        async def seed(repo: Repository, **kwargs: object) -> DiscoverSeedResult:
            work = repo.upsert_provider_record(record(), overwrite=False)
            return DiscoverSeedResult(
                work_id=work.id, created=True, title=work.title, primary_code=work.primary_code, fallback=None
            )

        async def submit(url: str, **kwargs: object) -> dict[str, object]:
            return {"info_hash": info_hash_from_uri(url), "remote_task_id": "fixture"}

        monkeypatch.setattr(runtime.discover, "seed", seed)
        monkeypatch.setattr(runtime.pan_service, "status", lambda: {"connected": True, "offline_ready": True})
        monkeypatch.setattr(runtime.pan_service, "backend", lambda: "openlist")
        monkeypatch.setattr(runtime.pan_service, "offline_target", lambda: "/REPLACE_OFFLINE")
        monkeypatch.setattr(runtime.pan_service, "submit_offline_url", AsyncMock(side_effect=submit))
        test_client.headers.update(
            {
                "Authorization": "Bearer REPLACE_TEST_TOKEN",
                "X-Catalog-Owner": "test-owner",
            }
        )
        yield test_client


def call(client: TestClient, name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = client.post("/api/skill/v1/call", json={"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["ok"], payload
    result = payload["result"]
    assert isinstance(result, dict)
    return result


def wait_task(client: TestClient, task_id: str) -> dict[str, object]:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        task = call(client, "tasks.get", {"task_id": task_id})
        if task["finished_at"]:
            return task
        time.sleep(0.025)
    pytest.fail("local fixture task did not complete")


def test_preview_is_read_only_and_cannot_be_switched_to_submit(client: TestClient) -> None:
    assert call(client, "intake.status", {"code": "SSIS-123"})["in_catalog"] is False
    assert call(client, "intake.code.preview", {"code": "SSIS-123"})["action"] == "planned"
    assert call(client, "intake.magnet.inspect", {"magnet": MAGNET})["code"] == "SSIS-123"
    assert call(client, "intake.magnet.preview", {"magnet": MAGNET})["dry_run"] is True
    no_code = f"magnet:?xt=urn:btih:{'C' * 40}&dn=Personal+clip"
    preview = call(client, "intake.magnet.preview", {"magnet": no_code})
    assert preview["code"] is None and preview["action"] == "planned"
    for name in ("intake.code.preview", "intake.magnet.preview"):
        args: dict[str, object] = {"dry_run": False}
        args.update({"code": "SSIS-123"} if name == "intake.code.preview" else {"magnet": MAGNET})
        assert client.post("/api/skill/v1/call", json={"name": name, "arguments": args}).status_code == 422
    with app.state.runtime.database.session() as session:
        repo = Repository(session)
        assert repo.list_works() == [] and repo.list_pan_offline_tasks() == []
    app.state.runtime.pan_service.submit_offline_url.assert_not_called()
    assert app.state.runtime.library_prefs_store.load().want_list == []


def test_number_intake_is_deduplicated_and_completion_is_reported_separately(client: TestClient) -> None:
    first = call(client, "intake.code.submit", {"code": "SSIS-123"})
    assert first["action"] == "submitted" and first["dry_run"] is False
    second = call(client, "intake.code.submit", {"code": "ssis123"})
    assert second["action"] == "running" and second["job_id"] == first["job_id"]
    app.state.runtime.pan_service.submit_offline_url.assert_awaited_once()
    state = call(client, "intake.status", {"code": "SSIS-123"})
    assert state["in_catalog"] is True and state["in_library"] is False
    job = call(client, "intake.job", {"job_id": first["job_id"]})
    assert job["status"] == "running" and job["stage"] != "in_library"
    jobs = client.post("/api/skill/v1/call", json={"name": "intake.jobs", "arguments": {}}).json()
    assert jobs["ok"] and jobs["result"][0]["job_id"] == first["job_id"]
    with app.state.runtime.database.session() as session:
        repo = Repository(session)
        task = repo.get_pan_offline_task(str(first["job_id"]))
        assert task is not None
        repo.update_pan_offline_task(task, status="done", strm_path="/REPLACE_EXPORT/SSIS-123.strm")
    done = call(client, "intake.job", {"job_id": first["job_id"]})
    assert done["stage"] == "in_library" and done["strm_path"]
    assert call(client, "intake.status", {"code": "SSIS-123"})["in_library"] is True
    assert call(client, "intake.code.submit", {"code": "SSIS-123"})["action"] == "exists"
    app.state.runtime.pan_service.submit_offline_url.assert_awaited_once()


def test_code_less_magnet_uses_hash_identity_without_inventing_a_number(client: TestClient) -> None:
    magnet = f"magnet:?xt=urn:btih:{'C' * 40}&dn=Personal+clip"
    first = call(client, "intake.magnet.submit", {"magnet": magnet})
    assert first["code"] is None and first["action"] == "submitted"
    second = call(client, "intake.magnet.submit", {"magnet": magnet})
    assert first["job_id"] == second["job_id"] and first["work_id"] == second["work_id"]
    app.state.runtime.pan_service.submit_offline_url.assert_awaited_once()
    detail = call(client, "works.detail", {"work_id": first["work_id"]})
    assert detail["primary_code"] is None and detail["title"] == "Personal clip"
    assert isinstance(detail["tags"], list) and "magnet-ingest" in detail["tags"]


def test_no_resource_means_hunting_not_a_completed_import(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(app.state.runtime.discover, "list_magnets", AsyncMock(return_value=()))
    result = call(client, "intake.code.submit", {"code": "SSIS-123"})
    assert result["action"] == "hunting" and result["job_id"] is None
    assert result["work_id"] in app.state.runtime.library_prefs_store.load().want_list
    app.state.runtime.pan_service.submit_offline_url.assert_not_called()


def test_metadata_only_lookup_does_not_submit_a_download(client: TestClient) -> None:
    result = call(client, "works.lookup", {"code": "SSIS-123"})
    assert isinstance(result["work"], dict)
    assert result["work"]["primary_code"] == "SSIS-123"
    work_id = result["work"]["id"]
    assert call(client, "works.refresh", {"work_id": work_id})["accepted_work_id"] == work_id
    app.state.runtime.pan_service.submit_offline_url.assert_not_called()
    for arguments in (
        {},
        {"code": "SSIS-123", "source_url": "https://example.com/detail"},
        {"code": "SSIS-123", "arbitrary": True},
    ):
        response = client.post("/api/skill/v1/call", json={"name": "works.lookup", "arguments": arguments})
        assert response.status_code == 422


def test_domain_intake_failure_is_not_reported_as_success(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        app.state.runtime.pan_service, "status", lambda: {"connected": True, "offline_ready": False}
    )
    response = client.post(
        "/api/skill/v1/call",
        json={
            "name": "intake.code.submit",
            "arguments": {"code": "SSIS-123"},
        },
    )
    assert response.status_code == 200
    assert response.json()["ok"] is False and response.json()["result"]["action"] == "error"
    app.state.runtime.pan_service.submit_offline_url.assert_not_called()


def test_local_scan_paging_identify_and_selected_candidate_preserve_original_files(
    client: TestClient,
    tmp_path: Path,
) -> None:
    folder = tmp_path / "library"
    folder.mkdir()
    original: dict[Path, bytes] = {}
    for code in ("SSIS-123", "OTHER-999"):
        path = folder / (code + ".strm")
        original[path] = b"https://example.com/media.mp4\n"
        path.write_bytes(original[path])
    library = client.post("/api/libraries", json={"name": "Fixture", "root_path": str(folder)}).json()["id"]
    listing = client.post("/api/skill/v1/call", json={"name": "libraries.list", "arguments": {}}).json()
    assert listing["ok"] and listing["result"][0]["id"] == library
    queued = call(client, "libraries.scan", {"library_id": library})
    assert wait_task(client, str(queued["id"]))["status"] == "succeeded"
    first = call(client, "libraries.assets", {"library_id": library, "limit": 1})
    second = call(client, "libraries.assets", {"library_id": library, "limit": 1, "offset": 1})
    assert first["total"] == second["total"] == 2
    assert isinstance(first["items"], list) and isinstance(second["items"], list)
    items = first["items"] + second["items"]
    assert items[0]["id"] != items[1]["id"]
    asset = next(item for item in items if Path(item["path"]).stem == "SSIS-123")
    identified = call(client, "assets.identify", {"asset_id": asset["id"]})
    assert identified["accepted_work_id"] is not None
    assert call(client, "assets.get", {"asset_id": asset["id"]})["work_id"] == identified["accepted_work_id"]
    other = next(item for item in items if item["id"] != asset["id"])
    manual = client.post("/api/assets/" + other["id"] + "/manual-candidate", json={"title": "User selection"})
    assert manual.status_code == 201
    candidates = client.post(
        "/api/skill/v1/call",
        json={
            "name": "assets.candidates",
            "arguments": {"asset_id": other["id"]},
        },
    ).json()
    assert candidates["ok"] and candidates["result"][0]["id"] == manual.json()["id"]
    assert call(client, "assets.get", {"asset_id": other["id"]})["work_id"] is None
    accepted = call(client, "candidates.accept", {"candidate_id": manual.json()["id"]})
    assert call(client, "assets.get", {"asset_id": other["id"]})["work_id"] == accepted["id"]
    queued = call(client, "libraries.identify", {"library_id": library, "limit": 2})
    assert wait_task(client, str(queued["id"]))["finished_at"]
    assert all(path.read_bytes() == data for path, data in original.items())


def test_intake_routes_require_auth_and_inputs_stay_bounded(client: TestClient) -> None:
    assert client.get("/api/intake/v1/jobs", headers={"Authorization": "Bearer wrong"}).status_code == 401
    for name, arguments in (
        ("libraries.assets", {"library_id": "../tasks"}),
        ("libraries.assets", {"library_id": "fixture", "limit": 101}),
        ("libraries.identify", {"library_id": "fixture", "limit": 501}),
        ("assets.identify", {"asset_id": "../tasks"}),
        ("intake.job", {"job_id": "../jobs"}),
        ("intake.magnet.submit", {"magnet": MAGNET, "dry_run": True}),
    ):
        response = client.post("/api/skill/v1/call", json={"name": name, "arguments": arguments})
        assert response.status_code == 422
    missing = client.post(
        "/api/skill/v1/call",
        json={
            "name": "libraries.assets",
            "arguments": {"library_id": "missing"},
        },
    ).json()
    assert not missing["ok"] and missing["status"] == 404
