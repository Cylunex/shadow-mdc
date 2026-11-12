from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Work


def test_runtime_edit_fill_guards_and_catalog_projection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="empty", title="Keep", actors=["Keep actor"], tags=["Keep tag"]),
                Work(id="locked", title="Locked", field_locks=["runtime_seconds"]),
            ])
        client.headers.update({"Authorization": "Bearer REPLACE_TEST", "X-Catalog-Owner": "test-owner"})
        path = "/api/skill/v1/call"
        schema = client.get("/api/skill/v1/catalog").json()
        capability = next(c for c in schema["capabilities"] if c["name"] == "works.update")
        assert "runtime_seconds" in capability["input_schema"]["properties"]
        for value in (0, -1, 3.5, True, "3600"):
            rejected = client.post(path, json={"name": "works.update", "arguments": {
                "work_id": "empty", "runtime_seconds": value,
            }})
            assert rejected.status_code == 422
        for key in ("empty", "locked"):
            result = client.post(path, json={"name": "works.update", "arguments": {
                "work_id": key, "runtime_seconds": 5400, "only_missing": True, "lock_edited": False,
            }}).json()
            assert result["ok"]
            assert result["result"]["runtime_seconds"] == (5400 if key == "empty" else None)
        protected = client.post(path, json={"name": "works.update", "arguments": {
            "work_id": "empty", "runtime_seconds": 6000, "title": "Do not overwrite", "only_missing": True,
        }}).json()["result"]
        assert protected["runtime_seconds"] == 5400 and protected["title"] == "Keep"
        assert protected["actors"] == ["Keep actor"] and protected["tags"] == ["Keep tag"]
        assert "runtime_seconds" not in protected["field_locks"]
        cards = client.post("/api/catalog/works/query", json={}).json()["items"]
        assert next(c for c in cards if c["id"] == "empty")["duration_ms"] == 5400000
        edited = client.patch("/api/works/empty", json={"runtime_seconds": 7200}).json()
        assert edited["runtime_seconds"] == 7200 and "runtime_seconds" in edited["field_locks"]
