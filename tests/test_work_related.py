from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory


def test_related_works_by_actor_and_tag(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            shared = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="rel-1",
                    title="Shared Actor Title",
                    code="TEST-001",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    actors=("Alice", "Bob"),
                    tags=("巨乳", "黑丝", "non-jav-seed"),
                ),
                overwrite=True,
            )
            same_actor = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="rel-2",
                    title="Same Actor Other",
                    code="TEST-002",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    actors=("Alice",),
                    tags=("制服",),
                ),
                overwrite=True,
            )
            same_tag = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id="rel-3",
                    title="Same Tag Other",
                    code="TEST-003",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    actors=("Carol",),
                    tags=("巨乳", "美腿"),
                ),
                overwrite=True,
            )
            session.commit()
            shared_id = shared.id
            same_actor_id = same_actor.id
            same_tag_id = same_tag.id

        response = client.get(f"/api/works/{shared_id}/related", params={"limit": 12})
        assert response.status_code == 200
        payload = response.json()
        actor_ids = [item["id"] for item in payload["by_actor"]]
        tag_ids = [item["id"] for item in payload["by_tag"]]
        assert same_actor_id in actor_ids
        assert shared_id not in actor_ids
        assert shared_id not in tag_ids
        assert same_tag_id in tag_ids
