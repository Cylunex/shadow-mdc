import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc import api
from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.providers.r18_dump import R18ServiceConfig, R18ServiceProvider

CONFIG = b'{"base_url":"http://r18.example.com/service","token":"fixture-key"}'
RECORD = (Path(__file__).parent / "fixtures/r18-service/record.json").read_bytes()


def test_remote_record_through_metadata_and_magnet_routes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_file = tmp_path / "r18-service.local.json"
    config_file.write_bytes(CONFIG)
    monkeypatch.setenv("SHADOW_MDC_R18_SERVICE_CONFIG", str(config_file))
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "test-mdc-key")
    monkeypatch.setenv("HTTP_PROXY", "http://proxy.example.com")
    calls: list[httpx.Request] = []

    def lookup(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        assert str(request.url) == "http://r18.example.com/service/lookup?q=FIXTURE-123"
        assert request.headers["Authorization"] == "Bearer fixture-key"
        return httpx.Response(200, content=RECORD)

    def provider(config: R18ServiceConfig) -> R18ServiceProvider:
        return R18ServiceProvider(config, transport=httpx.MockTransport(lookup))

    monkeypatch.setattr("shadow_mdc.providers.r18_dump.R18ServiceProvider", provider)
    headers = {"Authorization": "Bearer test-mdc-key"}
    with TestClient(api.app) as client:
        for selection in ("", "?providers=r18dump", "?providers=false"):
            response = client.get("/api/intake/v1/metadata/FIXTURE-123" + selection, headers=headers)
            assert response.status_code == 200
            assert response.json()["r18dump"]["release_date"] == "2026-11-24"
            assert response.json()["in_catalog"] is False
        assert len(calls) == 3

        inspected = client.post(
            "/api/intake/v1/magnet/inspect",
            headers=headers,
            json={"magnet": f"magnet:?xt=urn:btih:{'1' * 40}&dn=FIXTURE-123"},
        )
        assert inspected.status_code == 200
        assert inspected.json()["preview_url"] == "https://images.example.com/fixture.jpg"
        assert len(calls) == 4

        preview = client.post(
            "/api/intake/v1/offline",
            headers=headers,
            json={"code": "FIXTURE-123", "dry_run": True, "magnet": f"magnet:?xt=urn:btih:{'2' * 40}"},
        )
        assert preview.status_code == 200
        assert preview.json()["metadata"]["source"] == "r18dump"
        assert len(calls) == 5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("statuses", "body", "reason", "hits"),
    [
        ([404], b"", None, 0),
        ([503, 200], RECORD, None, 1),
        ([503, 503], b"", "network", 0),
        ([401], b"", "authentication", 0),
        ([403], b"", "authentication", 0),
        ([307], b"", "http", 0),
        ([200], b'{"unexpected":true}', "invalid_response", 0),
        ([200], RECORD.replace(b"FIXTURE-123", b"OTHER-123"), "invalid_response", 0),
    ],
)
async def test_lookup_status_and_response_contract(
    statuses: list[int], body: bytes, reason: str | None, hits: int
) -> None:
    replies = iter(statuses)
    lookup = AsyncMock(side_effect=lambda request: httpx.Response(next(replies), content=body))
    provider = R18ServiceProvider(
        R18ServiceConfig.model_validate_json(CONFIG), transport=httpx.MockTransport(lookup)
    )
    registry = ProviderRegistry([provider])
    hints = IdentityHints(
        mode=QueryMode.CODE, term="fixture-123", code="fixture-123", family=ContentFamily.JAV
    )
    try:
        batch = await registry.search(hints)
        assert len(batch.records) == hits
        assert [failure.reason for failure in batch.failures] == ([reason] if reason else [])
        assert lookup.await_count == len(statuses)
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_invalid_and_fc2_codes_do_not_send_requests() -> None:
    lookup = AsyncMock()
    provider = R18ServiceProvider(
        R18ServiceConfig.model_validate_json(CONFIG), transport=httpx.MockTransport(lookup)
    )
    try:
        for term in ("unknown", "FC2-PPV-123456"):
            hints = IdentityHints(mode=QueryMode.CODE, term=term, code=term, family=ContentFamily.JAV)
            assert await provider.search(hints) == []
        lookup.assert_not_awaited()
    finally:
        await provider.aclose()


@pytest.mark.asyncio
async def test_total_deadline_retries_and_registry_cools_down() -> None:
    async def stalled(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(30)
        raise AssertionError("lookup should have timed out")

    lookup = AsyncMock(side_effect=stalled)
    config = R18ServiceConfig.model_validate_json(CONFIG)
    provider = R18ServiceProvider(
        config.model_copy(update={"attempt_timeout_seconds": 0.1}),
        transport=httpx.MockTransport(lookup),
    )
    registry = ProviderRegistry([provider], failure_threshold=1)
    hints = IdentityHints(
        mode=QueryMode.CODE, term="FIXTURE-123", code="FIXTURE-123", family=ContentFamily.JAV
    )
    try:
        first = await registry.search(hints)
        assert first.failures[0].reason == "timeout"
        second = await registry.search(hints)
        assert second.failures[0].reason == "cooldown"
        assert lookup.await_count == 2
    finally:
        await provider.aclose()
