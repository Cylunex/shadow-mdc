"""Ranked local library search helpers and /api/works/search."""

from __future__ import annotations

from dataclasses import dataclass, replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.domain import IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.services.library_search import rank_work_dicts, score_work_fields


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


def test_score_work_fields_prefers_exact_code() -> None:
    hit = score_work_fields(
        "SSIS-001",
        work_id="w1",
        title="Other Title",
        original_title=None,
        primary_code="SSIS-001",
        actors=["Someone"],
        studio="S1",
        series=None,
        tags=["巨乳"],
    )
    assert hit is not None
    assert hit.score == 100.0
    assert hit.reason == "code_exact"


def test_score_work_fields_matches_actor_and_title() -> None:
    actor_hit = score_work_fields(
        "三上悠亚",
        work_id="w2",
        title="Random",
        original_title=None,
        primary_code="ABC-123",
        actors=["三上悠亚"],
        studio=None,
        series=None,
        tags=[],
    )
    assert actor_hit is not None
    assert actor_hit.score >= 70.0

    title_hit = score_work_fields(
        "特浓",
        work_id="w3",
        title="特浓中出专场",
        original_title=None,
        primary_code=None,
        actors=[],
        studio=None,
        series=None,
        tags=[],
    )
    assert title_hit is not None
    assert title_hit.score >= 48.0


def test_rank_work_dicts_orders_by_score() -> None:
    rows = [
        {
            "id": "a",
            "title": "Something with SSIS-001 mentioned",
            "primary_code": "OTHER-1",
            "actors": [],
            "tags": [],
        },
        {
            "id": "b",
            "title": "Exact",
            "primary_code": "SSIS-001",
            "actors": [],
            "tags": [],
        },
    ]
    page, total = rank_work_dicts("SSIS-001", rows, limit=10, offset=0)
    assert total == 2
    assert page[0]["id"] == "b"


def test_works_search_endpoint_ranks_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'search.db'}")
    monkeypatch.delenv("SHADOW_MDC_REDIS_URL", raising=False)

    records = [
        ProviderRecord(
            provider="fixture",
            external_id="s1",
            code="SSIS-001",
            title="First Match Title",
            family=ContentFamily.JAV,
            actors=("Actor A",),
        ),
        ProviderRecord(
            provider="fixture",
            external_id="s2",
            code="OTHER-9",
            title="Mentions SSIS-001 in plot-like title",
            family=ContentFamily.JAV,
            actors=("Actor B",),
        ),
    ]

    class MultiProvider:
        @property
        def descriptor(self) -> ProviderDescriptor:
            return ProviderDescriptor(
                id="fixture",
                name="Multi",
                query_modes=frozenset({QueryMode.CODE}),
                families=frozenset({ContentFamily.JAV}),
            )

        async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
            code = (hints.code or hints.term or "").upper()
            return [item for item in records if (item.code or "").upper() == code]

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([MultiProvider()]),
        )
        client.post("/api/works/lookup", json={"code": "SSIS-001"})
        client.post("/api/works/lookup", json={"code": "OTHER-9"})
        app.state.runtime.response_cache.invalidate_prefix("works:list:")
        client.get("/api/works")  # warm lean cache
        payload = client.get("/api/works/search", params={"q": "SSIS-001"}).json()
        assert payload["total"] >= 1
        assert payload["items"][0]["primary_code"] == "SSIS-001"
