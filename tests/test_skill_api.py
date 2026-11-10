"""Authenticated skill capability discovery and allowlisted domain execution."""

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Work


def test_skill_auth_catalog_reads_and_idempotent_domain_changes(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'skill.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST_TOKEN")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add(Work(id="work", title="Fixture", family="jav", tags=["Amateur"]))
        path = "/api/skill/v1"
        assert client.get(path + "/catalog").status_code == 401
        assert client.get(path + "/catalog", headers={"Authorization": "Bearer wrong"}).status_code == 401
        client.headers.update({"Authorization": "Bearer REPLACE_TEST_TOKEN", "X-Catalog-Owner": "test-owner"})
        catalog = client.get(path + "/catalog").json()
        capabilities = {item["name"]: item for item in catalog["capabilities"]}
        assert len(capabilities) == 32
        assert {key for key, item in capabilities.items() if item["effect"] == "write"} == {
            "works.want",
            "classifications.assign",
            "intake.code.submit",
            "intake.magnet.submit",
            "works.lookup",
            "works.refresh",
            "libraries.scan",
            "libraries.identify",
            "assets.identify",
            "candidates.accept",
        }
        assert "work_id" in capabilities["works.detail"]["input_schema"]["required"]

        def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
            response = client.post(path + "/call", json={"name": name, "arguments": arguments})
            assert response.status_code == 200, response.text
            return response.json()  # type: ignore[no-any-return]

        page = call("works.query", {"tags": ["tag:amateur"], "limit": 1})
        assert page["ok"] and page["status"] == 200
        assert isinstance(page["result"], dict) and page["result"]["total"] == 1
        assert call("tags.list", {"only_with_works": True})["ok"]
        assert call("classifications.list", {})["ok"]
        assert call("actors.query", {"limit": 1})["ok"]
        assert call("playlists.list", {})["result"] == []
        assert call("tasks.list", {"limit": 2})["ok"]
        assert call("works.facets", {})["ok"]
        assert call("system.health", {})["ok"]
        assert call("classifications.assign", {"work_id": "work", "languages": ["languages:ja"]})["ok"]
        assert call("classifications.assign", {"work_id": "work", "languages": ["languages:ja"]})["ok"]
        taxonomy = call("classifications.work", {"work_id": "work"})
        assert isinstance(taxonomy["result"], dict) and taxonomy["result"]["languages"] == ["languages:ja"]
        assert call("works.want", {"work_id": "work", "wanted": True})["ok"]
        assert call("works.want", {"work_id": "work", "wanted": True})["ok"]
        detail = call("works.detail", {"work_id": "work"})
        assert isinstance(detail["result"], dict) and detail["result"]["want_list"] is True
        assert call("works.want", {"work_id": "work", "wanted": False})["ok"]
        detail = call("works.detail", {"work_id": "work"})
        assert isinstance(detail["result"], dict) and detail["result"]["want_list"] is False
        rejected = call("works.want", {"work_id": "missing", "wanted": True})
        assert rejected["ok"] is False and rejected["status"] == 404
        assert "missing" not in app.state.runtime.library_prefs_store.load().want_list
        missing = call("works.detail", {"work_id": "missing"})
        assert missing["ok"] is False and missing["status"] == 404
        assert (
            client.post(path + "/call", json={"name": "arbitrary.http", "arguments": {}}).status_code == 404
        )
        invalid = client.post(
            path + "/call", json={"name": "works.detail", "arguments": {"work_id": "../tasks"}}
        )
        assert invalid.status_code == 422
        assert "../tasks" not in invalid.text
        assert (
            client.post(
                path + "/call", json={"name": "works.query", "arguments": {"url": "https://example.com"}}
            ).status_code
            == 422
        )


def test_skill_api_is_disabled_without_existing_service_credential(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'disabled.db'}")
    monkeypatch.delenv("SHADOW_MDC_MCP_TOKEN", raising=False)
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "")
    with TestClient(app) as client:
        assert client.get("/api/skill/v1/catalog").status_code == 503
