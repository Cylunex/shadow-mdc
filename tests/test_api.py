from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.domain import IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderRegistry


@dataclass(frozen=True)
class LookupProvider:
    record: ProviderRecord

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self.record.provider,
            name="Lookup fixture",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        return [self.record]


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
        tasks = client.get("/api/tasks").json()
        assert tasks[0]["kind"] == "scan"
        assert tasks[0]["status"] == "succeeded"
        assert tasks[0]["summary"]["discovered"] == 1

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


def test_library_sidecar_batch_preview_and_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    source = media_dir / "SONE-118.strm"
    source.write_text("https://media.example/SONE-118.mp4", encoding="utf-8")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "JAV", "root_path": str(media_dir), "category": "Japan"},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")

        preview = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "sidecar"},
        )
        assert preview.status_code == 200
        plan = preview.json()
        assert plan["asset_count"] == 1
        assert plan["samples"][0]["operations"][0]["destination"].endswith("SONE-118.nfo")

        applied = client.post(
            f"/api/libraries/{library['id']}/organize/apply",
            json={"mode": "sidecar", "token": plan["token"], "nfo_policy": "replace"},
        )
        assert applied.status_code == 200
        assert applied.json()["succeeded"] == 1
        assert "<uniqueid" in source.with_suffix(".nfo").read_text(encoding="utf-8")


def test_lookup_by_number_creates_work_without_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'lookup.db'}")
    record = ProviderRecord(
        provider="lookup-fixture",
        external_id="sone-118",
        code="SONE-118",
        title="Lookup title",
        family=ContentFamily.JAV,
        plot="Lookup plot",
    )

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        diagnostic = client.post("/api/providers/diagnose", json={"code": "SONE-118"})
        assert diagnostic.status_code == 200
        assert diagnostic.json()["diagnostics"][0]["status"] == "success"
        assert diagnostic.json()["diagnostics"][0]["accepted"] == 1
        response = client.post("/api/works/lookup", json={"code": "sone-118"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["matched_records"] == 1
        assert payload["work"]["primary_code"] == "SONE-118"
        assert payload["work"]["plot"] == "Lookup plot"
        assert client.get("/api/assets").json() == []
        assert client.get("/api/tasks").json()[0]["kind"] == "lookup"
