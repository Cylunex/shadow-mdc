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


def test_identity_alias_settings_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")
    payload = {
        "studios": {"xb": "杏吧传媒"},
        "series": {"小宝": "小宝探花"},
        "actors": {"演员别名": "规范姓名"},
    }

    with TestClient(app) as client:
        defaults = client.get("/api/settings/identity-aliases")
        assert defaults.status_code == 200
        assert "小宝探花" in defaults.json()["series"].values()

        saved = client.put("/api/settings/identity-aliases", json=payload)
        assert saved.status_code == 200
        assert saved.json() == payload
        assert client.get("/api/settings/identity-aliases").json() == payload

    assert (data_dir / "identity-aliases.json").is_file()


def test_filter_words_settings_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        defaults = client.get("/api/settings/filter-words")
        assert defaults.status_code == 200
        assert "社 區 最 新 情 報" in defaults.json()["words"]
        assert "sample" in defaults.json()["words"]

        saved = client.put(
            "/api/settings/filter-words",
            json={"words": ["自定义广告", "自定义广告", " trailer "]},
        )
        assert saved.status_code == 200
        assert saved.json() == {"words": ["自定义广告", "trailer"]}
        assert client.get("/api/settings/filter-words").json() == saved.json()

    assert (data_dir / "filter-words.txt").read_text(encoding="utf-8") == "自定义广告\ntrailer\n"


def test_no_code_asset_can_create_and_accept_local_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media" / "小宝探花"
    media_dir.mkdir(parents=True)
    (media_dir / "01.strm").write_text(
        "https://media.example/本地建档标题.mp4?token=secret",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "No code", "root_path": str(media_dir.parent), "recursive": True},
        )
        assert library.status_code == 201
        scan = client.post(f"/api/libraries/{library.json()['id']}/scan")
        assert scan.status_code == 200
        asset = client.get("/api/assets").json()[0]

        created = client.post(
            f"/api/assets/{asset['id']}/manual-candidate",
            json={"studio": "本地片商", "actors": ["人物甲"], "tags": ["自定义"]},
        )
        assert created.status_code == 201
        candidate = created.json()
        assert candidate["provider"] == "local-manual"
        assert candidate["decision"] == "review"
        assert candidate["record"]["title"] == "本地建档标题"
        assert candidate["record"]["studio"] == "本地片商"

        accepted = client.post(f"/api/candidates/{candidate['id']}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["title"] == "本地建档标题"
        assert accepted.json()["actors"] == ["人物甲"]

        missing = client.post("/api/assets/missing/manual-candidate", json={"title": "Title"})
        assert missing.status_code == 404
