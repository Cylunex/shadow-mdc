from pathlib import Path

import httpx
import pytest
from sqlalchemy import select

from shadow_mdc.db.models import SourceSnapshot
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily
from shadow_mdc.services.translation import GoogleTitleTranslator, TranslationCache, needs_translation


@pytest.mark.asyncio
async def test_title_translation_preserves_source_snapshot_and_uses_cache(tmp_path: Path) -> None:
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        assert request.url.params["sl"] == "auto"
        assert request.url.params["tl"] == "zh-CN"
        return httpx.Response(
            200,
            json=[[["邻居的垃圾房", "隣人のゴミ部屋", None, None]], None, "ja"],
        )

    database = Database(f"sqlite:///{tmp_path / 'translation.db'}")
    database.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        translator = GoogleTitleTranslator(
            client,
            TranslationCache(tmp_path / "translations.db"),
            enabled=True,
            endpoint="https://translate.example/translate_a/single",
            target_language="zh-CN",
        )
        with database.session() as session:
            repo = Repository(session)
            source = "隣人のゴミ部屋"
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="fixture-1",
                    code="TEST-001",
                    title=source,
                    original_title=source,
                    family=ContentFamily.JAV,
                    language="ja",
                ),
                overwrite=True,
            )

            first = await translator.translate_work(repo, work)
            second = await translator.translate_work(repo, work)

            assert first.status == "translated"
            assert second.status == "translated"
            assert requests == 1
            assert work.title == "邻居的垃圾房"
            assert work.original_title == source
            assert work.field_sources["title"] == "translation:google"
            snapshot = session.scalar(select(SourceSnapshot).where(SourceSnapshot.work_id == work.id))
            assert snapshot is not None
            assert snapshot.payload["title"] == source


def test_translation_language_gate_skips_existing_chinese() -> None:
    assert needs_translation("这是中文标题", "jav") is False
    assert needs_translation("隣人のゴミ部屋", "jav") is True
    assert needs_translation("Korean title", "western") is True
    assert needs_translation("English title", "chinese") is False


@pytest.mark.asyncio
async def test_local_catalog_title_is_not_translated(tmp_path: Path) -> None:
    requests = 0

    def handle(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(200, json=[[["不应调用", str(request.url), None, None]]])

    database = Database(f"sqlite:///{tmp_path / 'translation.db'}")
    database.initialize()
    async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
        translator = GoogleTitleTranslator(
            client,
            TranslationCache(tmp_path / "translations.db"),
            enabled=True,
            endpoint="https://translate.example/translate_a/single",
            target_language="zh-CN",
        )
        with database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="local-path",
                    external_id="local-1",
                    title="meowsex_v3-5",
                    family=ContentFamily.UNKNOWN,
                ),
                overwrite=True,
            )

            result = await translator.translate_work(repo, work)

    assert result.status == "skipped"
    assert result.detail == "local title"
    assert requests == 0
