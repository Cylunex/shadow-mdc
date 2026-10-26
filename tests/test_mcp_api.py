"""/api/mcp/v1: token auth, library/metadata lookup, offline_add (mock 115 + magnets)."""

from __future__ import annotations

from datetime import date
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import WorkMagnet
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import Artwork, ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.media.magnets import MagnetLink
from shadow_mdc.providers.base import SearchBatch

TOKEN = "test-mcp-token-123"
AUTH = {"Authorization": f"Bearer {TOKEN}"}
HASH_GOOD = "B" * 40
HASH_BAD = "A" * 40


def _record(code: str = "SSIS-123", *, with_cover: bool = False) -> ProviderRecord:
    artwork = ()
    if with_cover:
        artwork = (Artwork(kind="cover", url=f"https://cdn.example/{code}/cover.jpg"),)
    return ProviderRecord(
        provider="javdb",
        external_id="x123",
        title="Fixture title",
        code=code,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        source_url="https://javdb.com/v/x123",
        actors=("Actress A",),
        release_date=date(2021, 1, 1),
        artwork=artwork,
    )


def _magnets() -> tuple[MagnetLink, ...]:
    return (
        MagnetLink(
            info_hash=HASH_BAD,
            uri=f"magnet:?xt=urn:btih:{HASH_BAD}",
            name="OTHER-999",
            has_subtitle=True,
            hd=True,
            size_bytes=9 << 30,
            provider="javdb",
        ),
        MagnetLink(
            info_hash=HASH_GOOD,
            uri=f"magnet:?xt=urn:btih:{HASH_GOOD}",
            name="SSIS-123-C",
            has_subtitle=True,
            hd=True,
            size_bytes=6 << 30,
            provider="javdb",
        ),
    )


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):  # type: ignore[no-untyped-def]
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'mcp.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", TOKEN)
    with TestClient(app) as test_client:
        rt = test_client.app.state.runtime
        monkeypatch.setattr(
            rt.providers, "search", AsyncMock(return_value=SearchBatch(records=(_record(),), failures=()))
        )
        monkeypatch.setattr(rt.discover, "list_magnets", AsyncMock(return_value=_magnets()))
        monkeypatch.setattr(rt.discover, "r18_record", lambda code: None)

        async def fake_seed(repo: Repository, **kwargs: object):  # type: ignore[no-untyped-def]
            from shadow_mdc.services.discover import DiscoverSeedResult

            work = repo.upsert_provider_record(_record(), overwrite=False)
            return DiscoverSeedResult(
                work_id=work.id, created=True, title=work.title, primary_code=work.primary_code, fallback=None
            )

        monkeypatch.setattr(rt.discover, "seed", fake_seed)
        pan = rt.pan_service
        submitted: list[str] = []

        async def fake_submit(url: str, **kwargs: object) -> dict[str, object]:
            submitted.append(url)
            return {"info_hash": HASH_GOOD, "remote_task_id": "t1"}

        monkeypatch.setattr(pan, "status", lambda: {"connected": True, "offline_ready": True})
        monkeypatch.setattr(pan, "backend", lambda: "openlist")
        monkeypatch.setattr(pan, "offline_target", lambda: "/115/云下载")
        monkeypatch.setattr(pan, "submit_offline_url", AsyncMock(side_effect=fake_submit))
        test_client.submitted = submitted  # type: ignore[attr-defined]
        yield test_client


def test_auth_required(client: TestClient) -> None:
    assert client.get("/api/mcp/v1/health").status_code == 401
    assert client.get("/api/mcp/v1/health", headers={"Authorization": "Bearer wrong"}).status_code == 401
    assert client.get("/api/mcp/v1/health", headers={"Authorization": f"Basic {TOKEN}"}).status_code == 401
    ok = client.get("/api/mcp/v1/health", headers=AUTH)
    assert ok.status_code == 200 and ok.json()["ok"] is True


def test_disabled_without_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'x.db'}")
    monkeypatch.delenv("SHADOW_MDC_MCP_TOKEN", raising=False)
    with TestClient(app) as c:
        assert c.get("/api/mcp/v1/health", headers=AUTH).status_code == 503


def test_invalid_code_422(client: TestClient) -> None:
    assert client.get("/api/mcp/v1/library/%E4%BD%A0%E5%A5%BD", headers=AUTH).status_code == 422


def test_metadata_and_library_not_in_catalog(client: TestClient) -> None:
    meta = client.get("/api/mcp/v1/metadata/ssis123", headers=AUTH).json()
    assert meta["code"] == "SSIS-123"
    assert meta["in_catalog"] is False
    assert meta["providers"][0]["title"] == "Fixture title"
    lib = client.get("/api/mcp/v1/library/SSIS-123", headers=AUTH).json()
    assert lib == {**lib, "in_catalog": False, "in_library": False}


def test_offline_dry_run_plans_without_writes(client: TestClient) -> None:
    out = client.post(
        "/api/mcp/v1/offline", json={"code": "ｓｓｉｓ－１２３", "dry_run": True}, headers=AUTH  # noqa: RUF001
    ).json()
    assert out["code"] == "SSIS-123"
    assert out["action"] == "planned"
    assert out["magnet"]["info_hash"] == HASH_GOOD
    assert out["work_id"] is None
    assert client.submitted == []  # type: ignore[attr-defined]
    rt = client.app.state.runtime
    with rt.database.session() as session:
        assert Repository(session).find_work_by_code("SSIS-123") is None


def test_offline_add_submits_then_idempotent(client: TestClient) -> None:
    first = client.post("/api/mcp/v1/offline", json={"code": "SSIS-123"}, headers=AUTH)
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["action"] == "submitted"
    job_id = body["job_id"]
    assert job_id and body["job"]["stage"] == "downloading"
    assert client.submitted and client.submitted[0].endswith(HASH_GOOD)  # type: ignore[attr-defined]

    again = client.post("/api/mcp/v1/offline", json={"code": "SSIS-123"}, headers=AUTH).json()
    assert again["action"] == "running" and again["job_id"] == job_id
    assert len(client.submitted) == 1  # type: ignore[attr-defined]

    job = client.get(f"/api/mcp/v1/jobs/{job_id}", headers=AUTH).json()
    assert job["code"] == "SSIS-123" and job["status"] == "running"
    jobs = client.get("/api/mcp/v1/jobs", headers=AUTH).json()
    assert [j["job_id"] for j in jobs] == [job_id]
    assert client.get("/api/mcp/v1/jobs/nope", headers=AUTH).status_code == 404

    # Simulate poller completion → in library with subtitle.
    rt = client.app.state.runtime
    strm_dir = Path(rt.settings.data_dir) / "strm" / "SSIS-123"
    strm_dir.mkdir(parents=True)
    (strm_dir / "SSIS-123.strm").write_text("x")
    (strm_dir / "SSIS-123.zh.srt").write_text("1")
    with rt.database.session() as session:
        repo = Repository(session)
        task = repo.get_pan_offline_task(job_id)
        assert task is not None
        task.status = "done"
        task.strm_path = str(strm_dir / "SSIS-123.strm")
    lib = client.get("/api/mcp/v1/library/SSIS-123", headers=AUTH).json()
    assert lib["in_library"] is True and lib["subtitle"] is True
    exists = client.post("/api/mcp/v1/offline", json={"code": "SSIS-123"}, headers=AUTH).json()
    assert exists["action"] == "exists"


def test_offline_add_hunting_goes_to_want_list(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = client.app.state.runtime
    monkeypatch.setattr(rt.discover, "list_magnets", AsyncMock(return_value=()))
    out = client.post("/api/mcp/v1/offline", json={"code": "SSIS-123"}, headers=AUTH).json()
    assert out["action"] == "hunting"
    assert out["work_id"] in rt.library_prefs_store.load().want_list


def test_offline_add_existing_magnet_rows(client: TestClient) -> None:
    rt = client.app.state.runtime
    with rt.database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(_record(), overwrite=True)
        session.add(
            WorkMagnet(
                work_id=work.id,
                provider="javdb",
                info_hash=HASH_GOOD,
                uri=f"magnet:?xt=urn:btih:{HASH_GOOD}",
                name="SSIS-123",
                size_bytes=1 << 30,
            )
        )
    out = client.post("/api/mcp/v1/offline", json={"code": "SSIS-123", "dry_run": True}, headers=AUTH).json()
    assert out["action"] == "planned" and out["work_id"]
    assert out["magnet"]["info_hash"] == HASH_GOOD


def test_subscribe_code_and_actor(client: TestClient) -> None:
    code = client.post("/api/mcp/v1/subscribe", json={"code": "SSIS-123"}, headers=AUTH).json()
    assert code["kind"] == "code" and code["created"] is True
    actor = client.post("/api/mcp/v1/subscribe", json={"actor": "Actress A"}, headers=AUTH).json()
    assert actor["created"] is True
    again = client.post("/api/mcp/v1/subscribe", json={"actor": "Actress A"}, headers=AUTH).json()
    assert again["created"] is False
    assert client.post("/api/mcp/v1/subscribe", json={}, headers=AUTH).status_code == 422


def test_rescrape_requires_catalog(client: TestClient) -> None:
    assert client.post("/api/mcp/v1/rescrape", json={"code": "SSIS-123"}, headers=AUTH).status_code == 404


HASH_MAG = "C" * 40


def _magnet_uri(info_hash: str, *, dn: str | None = None) -> str:
    uri = f"magnet:?xt=urn:btih:{info_hash}"
    if dn:
        from urllib.parse import quote

        uri = f"{uri}&dn={quote(dn)}"
    return uri


def test_offline_magnet_dry_run_jav_dn(client: TestClient) -> None:
    """dn with a clear 番号 reuses the code-based plan (scrape path), no writes."""

    magnet = _magnet_uri(HASH_MAG, dn="SSIS-123-C.mp4")
    out = client.post(
        "/api/mcp/v1/offline/magnet",
        json={"magnet": magnet, "dry_run": True},
        headers=AUTH,
    ).json()
    assert out["action"] == "planned"
    assert out["code"] == "SSIS-123"
    assert out["magnet"]["info_hash"] == HASH_MAG
    assert out["dry_run"] is True
    assert client.submitted == []  # type: ignore[attr-defined]
    rt = client.app.state.runtime
    with rt.database.session() as session:
        assert Repository(session).find_work_by_code("SSIS-123") is None


def test_offline_magnet_dry_run_non_jav(client: TestClient) -> None:
    magnet = _magnet_uri(HASH_MAG, dn="My Homemade Clip Vol.3")
    out = client.post(
        "/api/mcp/v1/offline/magnet",
        json={"magnet": magnet, "title": "Homemade Clip Special", "dry_run": True},
        headers=AUTH,
    ).json()
    assert out["action"] == "planned"
    assert out["code"] is None
    assert out["dry_run"] is True
    assert out["magnet"]["info_hash"] == HASH_MAG
    assert "non-JAV" in out["message"] or "magnet-ingest" in out["message"]
    assert out["metadata"]["title"] == "Homemade Clip Special"
    assert out["inspect"] is not None
    assert out["inspect"]["likely_non_jav"] is True
    assert out["inspect"]["info_hash"] == HASH_MAG
    assert client.submitted == []  # type: ignore[attr-defined]
    rt = client.app.state.runtime
    with rt.database.session() as session:
        assert Repository(session).find_work_by_provider_identity("magnet-ingest", HASH_MAG) is None


def test_offline_magnet_submit_non_jav_and_dedup(client: TestClient) -> None:
    magnet = _magnet_uri(HASH_MAG, dn="Western Scene NoCode")
    rt = client.app.state.runtime

    async def fake_submit(url: str, **kwargs: object) -> dict[str, object]:
        client.submitted.append(url)  # type: ignore[attr-defined]
        return {"info_hash": HASH_MAG, "remote_task_id": "tm1"}

    monkey_pan = rt.pan_service
    monkey_pan.submit_offline_url = AsyncMock(side_effect=fake_submit)  # type: ignore[method-assign]

    first = client.post(
        "/api/mcp/v1/offline/magnet",
        json={"magnet": magnet, "title": "Western Scene NoCode", "dry_run": False},
        headers=AUTH,
    )
    assert first.status_code == 200, first.text
    body = first.json()
    assert body["action"] == "submitted"
    assert body["code"] is None
    assert body["work_id"] and body["job_id"]
    assert body["metadata"]["title"] == "Western Scene NoCode"
    assert "magnet-ingest" in (body["metadata"]["tags"] or [])
    assert client.submitted and HASH_MAG in client.submitted[0]  # type: ignore[attr-defined]

    with rt.database.session() as session:
        work = Repository(session).find_work_by_provider_identity("magnet-ingest", HASH_MAG)
        assert work is not None
        assert work.family == "unknown"
        assert work.primary_code is None

    again = client.post(
        "/api/mcp/v1/offline/magnet",
        json={"magnet": magnet, "dry_run": False},
        headers=AUTH,
    ).json()
    assert again["action"] == "running"
    assert again["job_id"] == body["job_id"]
    assert len(client.submitted) == 1  # type: ignore[attr-defined]


def test_offline_magnet_code_override(client: TestClient) -> None:
    magnet = _magnet_uri(HASH_MAG, dn="totally-unrelated-name")
    out = client.post(
        "/api/mcp/v1/offline/magnet",
        json={"magnet": magnet, "code": "ｓｓｉｓ１２３", "dry_run": True},  # noqa: RUF001
        headers=AUTH,
    ).json()
    assert out["action"] == "planned"
    assert out["code"] == "SSIS-123"


def test_offline_magnet_rejects_bad_uri(client: TestClient) -> None:
    bad = client.post(
        "/api/mcp/v1/offline/magnet",
        json={"magnet": "https://example.com/not-a-magnet-link", "dry_run": True},
        headers=AUTH,
    )
    assert bad.status_code == 422

def test_magnet_inspect_jav_dn(client: TestClient) -> None:
    magnet = _magnet_uri(
        HASH_MAG,
        dn="SSIS-123-C.1080p.中字",
    ) + "&tr=udp%3A%2F%2Ftracker.example%3A80&xl=6442450944"
    out = client.post(
        "/api/mcp/v1/magnet/inspect",
        json={"magnet": magnet},
        headers=AUTH,
    ).json()
    assert out["info_hash"] == HASH_MAG
    assert out["code"] == "SSIS-123"
    assert out["likely_non_jav"] is False
    assert out["display_name"] and "SSIS-123" in out["display_name"]
    assert out["quality"]["has_subtitle"] is True
    assert out["quality"]["hd"] is True
    assert out["quality"]["size_bytes"] == 6442450944
    assert out["library_state"] == "absent"
    assert any("tracker.example" in t for t in out["trackers"])
    assert client.submitted == []  # type: ignore[attr-defined]


def test_magnet_inspect_non_jav_dn(client: TestClient) -> None:
    magnet = _magnet_uri(HASH_MAG, dn="My Homemade Clip Vol.3 VR")
    out = client.post(
        "/api/mcp/v1/magnet/inspect",
        json={"magnet": magnet, "title": "Homemade Clip Special"},
        headers=AUTH,
    ).json()
    assert out["code"] is None
    assert out["likely_non_jav"] is True
    assert out["title"] == "Homemade Clip Special"
    assert out["quality"]["vr"] is True
    assert out["library_state"] == "absent"
    assert out["preview_url"] is None  # no invent
    assert client.submitted == []  # type: ignore[attr-defined]


def test_magnet_inspect_known_code_with_poster(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    rt = client.app.state.runtime
    poster = "https://cdn.example/SSIS-123/cover.jpg"
    monkeypatch.setattr(
        rt.discover,
        "r18_record",
        lambda code: _record(code, with_cover=True) if code == "SSIS-123" else None,
    )
    magnet = _magnet_uri(HASH_MAG, dn="ssis123-4k-C")
    out = client.post(
        "/api/mcp/v1/magnet/inspect",
        json={"magnet": magnet},
        headers=AUTH,
    ).json()
    assert out["code"] == "SSIS-123"
    assert out["metadata"] is not None
    assert out["metadata"]["title"] == "Fixture title"
    assert out["metadata"]["actors"] == ["Actress A"]
    assert out["preview_url"] == poster
    assert out["quality"]["uhd_4k"] is True
    assert out["quality"]["has_subtitle"] is True
    assert client.submitted == []  # type: ignore[attr-defined]


def test_magnet_inspect_already_in_library(client: TestClient) -> None:
    rt = client.app.state.runtime
    magnet = _magnet_uri(HASH_MAG, dn="SSIS-123-C")
    # Seed catalog + running offline task for this hash
    with rt.database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(_record(with_cover=True), overwrite=False)
        repo.create_pan_offline_task(
            work_id=work.id,
            url=magnet,
            info_hash=HASH_MAG,
            directory_id="dir-inspect",
            remote_task_id="tm-inspect",
            status="running",
            backend="openlist",
        )
        session.commit()
        work_id = work.id

    out = client.post(
        "/api/mcp/v1/magnet/inspect",
        json={"magnet": magnet},
        headers=AUTH,
    ).json()
    assert out["code"] == "SSIS-123"
    assert out["library_state"] == "running"
    assert out["work_id"] == work_id
    assert out["job_id"]
    assert out["preview_url"] == "https://cdn.example/SSIS-123/cover.jpg"
    assert client.submitted == []  # type: ignore[attr-defined]

