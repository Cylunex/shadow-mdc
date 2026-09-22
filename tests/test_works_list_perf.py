"""Works list lean payload, batch collections, and cache key helpers."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import CollectionKind, ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.services.response_cache import TTL_WORKS, works_list_key


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


def test_works_list_is_lean_and_cached(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'lean.db'}")
    monkeypatch.delenv("SHADOW_MDC_REDIS_URL", raising=False)

    record = ProviderRecord(
        provider="fixture",
        external_id="lean-1",
        code="LEAN-001",
        title="Lean Card Title",
        plot="Should not appear in list payload",
        family=ContentFamily.JAV,
        actors=("Actor A", "Actor B"),
        tags=["巨乳", "中出"],
    )

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        work = client.post("/api/works/lookup", json={"code": "LEAN-001"}).json()["work"]
        work_id = work["id"]
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            stored = repo.get_work(work_id)
            assert stored is not None
            stored.reviews = [{"provider": "x", "text": "heavy"}]
            stored.field_sources = {**(stored.field_sources or {}), "plot": "fixture"}
            collection = repo.upsert_collection(name="Demo Series", kind=CollectionKind.SERIES)
            repo.link_work_collection(stored, collection)

        # Bust any post-lookup cache so lean serializer + collections batch run again.
        app.state.runtime.response_cache.invalidate_prefix("works:list:")

        rows = client.get("/api/works").json()
        card = next(item for item in rows if item["id"] == work_id)
        assert card["plot"] is None
        assert card["reviews"] == []
        assert card["identities"] == []
        assert card["artwork"] == []
        assert card["field_sources"] == {}
        assert card["title"] == "Lean Card Title"
        assert "Actor A" in card["actors"]
        assert any(item["name"] == "Demo Series" for item in card["collections"])

        detail = client.get(f"/api/works/{work_id}").json()
        assert detail["plot"] == "Should not appear in list payload"
        assert detail["reviews"]

        # Second list hit should succeed (cache path).
        again = client.get("/api/works").json()
        assert any(item["id"] == work_id for item in again)

    assert works_list_key(
        collection_id=None, collection_kind=None, collection=None, tags=()
    ).startswith("works:list:")
    assert TTL_WORKS == 120


def test_actors_endpoint_returns_profiles(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'actors-cache.db'}")
    monkeypatch.delenv("SHADOW_MDC_REDIS_URL", raising=False)

    record = ProviderRecord(
        provider="fixture",
        external_id="act-1",
        code="ACT-001",
        title="Actor Work",
        family=ContentFamily.JAV,
        actors=("Cache Actor",),
    )
    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        client.post("/api/works/lookup", json={"code": "ACT-001"})
        app.state.runtime.response_cache.invalidate_prefix("actors:")
        first = client.get("/api/actors").json()
        second = client.get("/api/actors").json()
        assert any(item["name"] == "Cache Actor" for item in first)
        assert len(first) == len(second)
