from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily
from shadow_mdc.media.nfo import parse_nfo, write_nfo, build_nfo
from shadow_mdc.media.parts import part_group_key
from shadow_mdc.services.field_priority import FieldPriorityStore, FieldPriorityConfig
from shadow_mdc.services.translation import (
    DeepLBackend,
    GoogleTitleTranslator,
    TranslationCache,
    build_translation_backends,
)


def test_part_group_key_collapses_cd_parts(tmp_path: Path) -> None:
    assert part_group_key(tmp_path / "ABC-123-CD1.mp4", "ABC-123") == part_group_key(
        tmp_path / "ABC-123-CD2.mp4", "ABC-123"
    )


def test_parse_nfo_roundtrip(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'nfo.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="1",
                code="NFO-001",
                title="NFO Title",
                family=ContentFamily.JAV,
            ),
            overwrite=True,
        )
        path = tmp_path / "movie.nfo"
        write_nfo(path, build_nfo(work, repo.identities_for_work(work.id)))
    parsed = parse_nfo(path)
    assert parsed["title"]
    assert parsed["code"] in {None, "NFO-001"} or "NFO" in str(parsed["code"])


def test_field_priority_store(tmp_path: Path) -> None:
    store = FieldPriorityStore(tmp_path / "field-priority.json")
    saved = store.save(FieldPriorityConfig(priorities={"title": ["local-manual", "javdb"]}))
    assert saved.priorities["title"][0] == "local-manual"
    assert "plot" in store.load().priorities


@pytest.mark.asyncio
async def test_deepl_backend_and_fallback(tmp_path: Path) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        if "deepl" in str(request.url):
            return httpx.Response(500, json={"message": "fail"})
        return httpx.Response(200, json=[[["你好", "hello", None, None]]])

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        backends = build_translation_backends(
            client,
            google_endpoint="https://translate.example/translate_a/single",
            deepl_api_url="https://api-free.deepl.com/v2/translate",
            deepl_api_key="x",
            deeplx_endpoint=None,
            custom_endpoint=None,
            prefer="deepl",
        )
        translator = GoogleTitleTranslator(
            client,
            TranslationCache(tmp_path / "translations.db"),
            enabled=True,
            endpoint="https://translate.example/translate_a/single",
            target_language="zh-CN",
            extra_backends=[b for b in backends if b.name != "google"],
        )
        database = Database(f"sqlite:///{tmp_path / 't.db'}")
        database.initialize()
        with database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="t1",
                    title="hello world",
                    family=ContentFamily.WESTERN,
                ),
                overwrite=True,
            )
            result = await translator.translate_work(repo, work)
            assert result.status == "translated"
            assert work.title == "你好"


def test_settings_and_lexicon_endpoints(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")
    with TestClient(app) as client:
        fp = client.get("/api/settings/field-priority")
        assert fp.status_code == 200
        assert "title" in fp.json()["priorities"]
        ms = client.get("/api/settings/media-server")
        assert ms.status_code == 200
        lex = client.get("/api/lexicon/export")
        assert lex.status_code == 200
        assert "exported_at" in lex.json()
        tasks = client.get("/api/tasks")
        assert tasks.status_code == 200


def test_scan_only_new_and_task_cancel(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    media = tmp_path / "media"
    media.mkdir()
    (media / "AAA-001.mp4").write_bytes(b"one")
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")
    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "lib", "root_path": str(media), "recognition_scope": "all"},
        ).json()
        first = client.post(f"/api/libraries/{library['id']}/scan", json={"only_new": False})
        assert first.status_code == 200
        assert first.json()["discovered"] >= 1
        second = client.post(f"/api/libraries/{library['id']}/scan", json={"only_new": True})
        assert second.status_code == 200
        assert second.json()["discovered"] == 0
        task_id = client.get("/api/tasks").json()[0]["id"]
        # finished tasks cannot cancel
        cancel = client.post(f"/api/tasks/{task_id}/cancel")
        assert cancel.status_code in {400, 409, 422}
