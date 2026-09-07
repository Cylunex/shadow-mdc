from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import CollectionKind, ContentFamily, MediaCategory
from shadow_mdc.services.collections import detect_collection_kind


def test_detect_collection_kind_prefers_chinese_platforms() -> None:
    assert detect_collection_kind("麻豆传媒", preferred=CollectionKind.STUDIO) is CollectionKind.PLATFORM
    assert detect_collection_kind("探花", preferred=CollectionKind.STUDIO) is CollectionKind.PLATFORM
    assert detect_collection_kind("糖心Vlog", preferred=CollectionKind.STUDIO) is CollectionKind.PLATFORM
    assert detect_collection_kind("Blacked", preferred=CollectionKind.STUDIO) is CollectionKind.STUDIO
    assert detect_collection_kind("MDX", preferred=CollectionKind.SERIES) is CollectionKind.SERIES


def test_seed_collections_from_work_fields(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'collections.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="c-1",
                title="国产合集样例",
                family=ContentFamily.CHINESE,
                category=MediaCategory.CHINA,
                studio="麻豆传媒",
                series="MDX",
                label="天美厂牌",
                actors=("演员甲",),
            ),
            overwrite=True,
        )
        result = repo.seed_collections_from_works()
        collections = repo.collections_for_work(work.id)
        kinds = {item.kind for item in collections}
        names = {item.name for item in collections}
        assert "platform" in kinds
        assert "series" in kinds
        assert "label" in kinds
        assert "麻豆传媒" in names
        assert "MDX" in names
        assert result["collections_total"] >= 3
        # Idempotent
        again = repo.seed_collections_from_works()
        assert again["collections_created"] == 0


def test_collections_api_list_and_filter_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="api-1",
                    title="平台作品",
                    family=ContentFamily.CHINESE,
                    category=MediaCategory.CHINA,
                    studio="探花",
                    series="KTV探花",
                    actors=("演员乙",),
                ),
                overwrite=True,
            )
            repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="api-2",
                    title="其它片商",
                    family=ContentFamily.WESTERN,
                    category=MediaCategory.EUROPE,
                    studio="Blacked",
                    actors=("Angela",),
                ),
                overwrite=True,
            )

        seeded = client.post("/api/collections/seed").json()
        assert seeded["collections_total"] >= 2
        platforms = client.get("/api/collections", params={"kind": "platform"}).json()
        tanhua = next(item for item in platforms if item["name"] == "探花")
        studios = client.get("/api/collections", params={"kind": "studio"}).json()
        assert any(item["name"] == "Blacked" for item in studios)

        # Lifespan may also seed curated non-JAV works that share the 探花 platform;
        # filter by collection_id and assert our fixture work is included.
        filtered = client.get("/api/works", params={"collection_id": tanhua["id"]}).json()
        titles = {item["title"] for item in filtered}
        assert "平台作品" in titles
        ours = next(item for item in filtered if item["title"] == "平台作品")
        assert any(item["name"] == "探花" for item in ours["collections"])
        blacked = client.get(
            "/api/works",
            params={"collection": "Blacked", "collection_kind": "studio"},
        ).json()
        assert any(item["title"] == "其它片商" for item in blacked)
