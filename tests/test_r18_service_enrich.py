import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from sqlalchemy import func, select

from shadow_mdc.db.models import SourceSnapshot, Work
from shadow_mdc.db.repository import Database
from shadow_mdc.providers.base import ProviderError
from shadow_mdc.providers.r18_dump import R18ServiceConfig, R18ServiceProvider
from shadow_mdc.services.r18_service_enrich import enrich_existing_jav_from_service

CONFIG = R18ServiceConfig.model_validate({"base_url": "https://r18.example.com", "token": "test-key"})
FIXTURE = json.loads((Path(__file__).parent / "fixtures/r18-service/record.json").read_text())


def hit(code: str) -> dict[str, Any]:
    record = {
        **FIXTURE, "code": code, "external_id": code.lower(),
        "studio": "Fixture studio", "plot": "Fixture synopsis", "language": "ja",
        "tags": ["Fixture tag"],
    }
    return {"query": code, "found": True, "record": record}


def miss(code: str) -> dict[str, Any]:
    return {"query": code, "found": False, "record": None}


@pytest.mark.asyncio
async def test_batch_preserves_order_duplicates_and_misses() -> None:
    queries = ("FIXTURE-123", "OTHER-999", "FIXTURE-123")
    attempts = 0

    def serve(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        assert request.url.path == "/videos/batch"
        assert request.method == "POST"
        assert request.headers["Authorization"] == "Bearer test-key"
        assert json.loads(request.content) == {"codes": list(queries)}
        attempts += 1
        if attempts == 1:
            return httpx.Response(503)
        return httpx.Response(200, json={"results": [hit(queries[0]), miss(queries[1]), hit(queries[2])]})

    provider = R18ServiceProvider(CONFIG, transport=httpx.MockTransport(serve))
    try:
        result = await provider.search_batch(queries)
        assert attempts == 2
        assert [item.record.code if item.record else None for item in result] == [
            queries[0], None, queries[2],
        ]
        assert not any(item.identity_conflict for item in result)
        for invalid in ((), ("FIXTURE-123",) * 101, ("FC2-123456",), ("unknown",)):
            with pytest.raises(ValueError):
                await provider.search_batch(invalid)
        assert attempts == 2
    finally:
        await provider.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "items",
    [
        [miss("OTHER-999"), hit("FIXTURE-123")],
        [hit("FIXTURE-123")],
        [{**hit("FIXTURE-123"), "found": False}, miss("OTHER-999")],
    ],
)
async def test_malformed_batch_is_a_failure(items: list[dict[str, Any]]) -> None:
    provider = R18ServiceProvider(
        CONFIG, transport=httpx.MockTransport(lambda _: httpx.Response(200, json={"results": items})),
    )
    try:
        with pytest.raises(ProviderError) as error:
            await provider.search_batch(("FIXTURE-123", "OTHER-999"))
        assert error.value.reason == "invalid_response"
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_backfill_dry_run_and_partial_failure_preserve_manual_fields(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    first = "FIXTURE-123"
    start = datetime(2026, 11, 27, tzinfo=UTC)
    with database.session() as session:
        work = Work(
            title="Existing title", family="jav", primary_code=first, created_at=start,
            actors=["Edited cast"], tags=["Existing tag"], field_locks=["plot"],
            artwork=[{"kind": "poster", "url": "https://images.example.com/local.jpg"}],
        )
        session.add(work)
        session.add_all(
            Work(
                title="Existing title", family="jav", primary_code=f"OTHER-{100 + idx}",
                created_at=start - timedelta(seconds=idx + 1),
            )
            for idx in range(100)
        )
        session.flush()
        work_id = work.id

    calls: list[list[str]] = []

    def serve(request: httpx.Request) -> httpx.Response:
        queries = json.loads(request.content)["codes"]
        calls.append(queries)
        if len(queries) == 1:
            return httpx.Response(503)
        results = [hit(code) if code == first else miss(code) for code in queries]
        results[1] = {**hit("OTHER-999"), "query": queries[1]}
        return httpx.Response(200, json={"results": results})

    provider = R18ServiceProvider(CONFIG, transport=httpx.MockTransport(serve))
    try:
        preview = await enrich_existing_jav_from_service(database, provider, dry_run=True, limit=100)
        assert preview.candidates == 100 and preview.matched == 1 and preview.updated == 0
        with database.session() as session:
            assert session.scalar(select(func.count()).select_from(SourceSnapshot)) == 0
        outcome = await enrich_existing_jav_from_service(database, provider)
        assert outcome.candidates == 101 and outcome.batches_completed == 1
        assert outcome.error and "503" in outcome.error
        assert outcome.conflicts == 1
        assert outcome.matched == outcome.updated == 1 and outcome.not_found == 98
        assert [len(query) for query in calls] == [100, 100, 1, 1]
    finally:
        await provider.aclose()

    with database.session() as session:
        work = session.get(Work, work_id)
        assert work is not None
        assert work.title == "Existing title" and work.actors == ["Edited cast"]
        assert work.plot is None and work.original_plot == "Fixture synopsis"
        assert work.studio == "Fixture studio" and work.tags == ["Existing tag"]
        assert work.artwork[0]["url"] == "https://images.example.com/local.jpg"
        assert work.field_sources["studio"] == "r18dump"
        assert session.scalar(select(func.count()).select_from(Work)) == 101
        assert session.scalar(select(func.count()).select_from(SourceSnapshot)) == 1
