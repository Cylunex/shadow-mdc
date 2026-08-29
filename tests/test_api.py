from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app


def test_library_scan_api_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "SSIS-123.mp4").write_bytes(b"not-a-real-video")

    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        created = client.post(
            "/api/libraries",
            json={"name": "Fixture", "root_path": str(media_dir), "recursive": True},
        )
        assert created.status_code == 201
        library_id = created.json()["id"]

        scan = client.post(f"/api/libraries/{library_id}/scan")
        assert scan.status_code == 200
        assert scan.json()["discovered"] == 1

        assets = client.get("/api/assets")
        assert assets.status_code == 200
        item = assets.json()[0]
        assert item["hints"]["code"] == "SSIS-123"
        assert item["hints"]["mode"] == "code"
