import asyncio
from dataclasses import dataclass

import httpx
import pytest

from shadow_mdc.domain import IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import HttpProvider, ProviderError, ProviderRegistry


@dataclass(frozen=True)
class FixtureProvider:
    provider_id: str
    result: ProviderRecord | None = None
    failure: ProviderError | None = None

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self.provider_id,
            name=self.provider_id,
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        if self.failure is not None:
            raise self.failure
        return [self.result] if self.result is not None else []


@dataclass
class CallCounter:
    active: int = 0
    maximum: int = 0


@dataclass(frozen=True)
class LimitedProvider:
    provider_id: str
    counter: CallCounter

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self.provider_id,
            name=self.provider_id,
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        self.counter.active += 1
        self.counter.maximum = max(self.counter.maximum, self.counter.active)
        await asyncio.sleep(0.01)
        self.counter.active -= 1
        return []


@pytest.mark.asyncio
async def test_provider_failure_does_not_hide_other_results() -> None:
    record = ProviderRecord(
        provider="healthy",
        external_id="1",
        code="SSIS-123",
        title="Fixture",
        family=ContentFamily.JAV,
    )
    registry = ProviderRegistry(
        [
            FixtureProvider("healthy", result=record),
            FixtureProvider("blocked", failure=ProviderError("blocked", "blocked", "challenge")),
        ]
    )

    batch = await registry.search(
        IdentityHints(term="SSIS-123", mode=QueryMode.CODE, family=ContentFamily.JAV, code="SSIS-123")
    )

    assert batch.records == (record,)
    assert batch.failures[0].provider == "blocked"
    assert batch.failures[0].reason == "blocked"


@pytest.mark.asyncio
async def test_ineligible_provider_is_not_called() -> None:
    registry = ProviderRegistry([FixtureProvider("jav")])
    batch = await registry.search(IdentityHints(term="free title", mode=QueryMode.TEXT))

    assert batch.records == ()
    assert batch.failures == ()


@pytest.mark.asyncio
async def test_repeated_network_failure_enters_temporary_cooldown() -> None:
    registry = ProviderRegistry(
        [FixtureProvider("offline", failure=ProviderError("offline", "network", "down"))],
        failure_threshold=1,
        cooldown_seconds=60,
    )
    hints = IdentityHints(
        term="SSIS-123",
        mode=QueryMode.CODE,
        family=ContentFamily.JAV,
        code="SSIS-123",
    )

    first = await registry.search(hints)
    second = await registry.search(hints)

    assert first.failures[0].reason == "network"
    assert second.failures[0].reason == "cooldown"


@pytest.mark.asyncio
async def test_provider_calls_are_globally_limited() -> None:
    counter = CallCounter()
    registry = ProviderRegistry(
        [LimitedProvider(f"source-{index}", counter) for index in range(6)],
        max_concurrent_calls=2,
    )

    await registry.search(
        IdentityHints(
            term="SSIS-123",
            mode=QueryMode.CODE,
            family=ContentFamily.JAV,
            code="SSIS-123",
        )
    )

    assert counter.maximum == 2


@pytest.mark.asyncio
async def test_http_provider_retries_transient_status() -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(503 if attempts < 3 else 200, text="ready", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = HttpProvider(client, retries=2)
        assert await provider._get_text("fixture", "https://example.test") == "ready"

    assert attempts == 3


@pytest.mark.asyncio
async def test_http_provider_reports_connect_timeout_without_empty_detail() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("", request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = HttpProvider(client, retries=0)
        with pytest.raises(ProviderError) as captured:
            await provider._get_text("fixture", "https://example.test")

    assert captured.value.reason == "connect_timeout"
    assert "ConnectTimeout" in captured.value.detail


@pytest.mark.asyncio
async def test_fc2_code_routes_to_fc2_specialist_providers() -> None:
    called: list[str] = []

    @dataclass(frozen=True)
    class TrackingProvider:
        provider_id: str
        modes: frozenset[QueryMode] = frozenset({QueryMode.CODE})
        families: frozenset[ContentFamily] = frozenset({ContentFamily.JAV})
        skip_fc2: bool = False

        @property
        def descriptor(self) -> ProviderDescriptor:
            return ProviderDescriptor(
                id=self.provider_id,
                name=self.provider_id,
                query_modes=self.modes,
                families=self.families,
            )

        async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
            called.append(self.provider_id)
            if self.skip_fc2 and (hints.code or "").startswith("FC2-"):
                return []
            return []

    registry = ProviderRegistry(
        [
            TrackingProvider("r18dev", skip_fc2=True),
            TrackingProvider("fc2club"),
            TrackingProvider("fc2hub"),
            TrackingProvider("fc2contents"),
            TrackingProvider("paipancon"),
            TrackingProvider("javdb"),
        ]
    )
    await registry.search(
        IdentityHints(
            term="FC2-3131319",
            mode=QueryMode.CODE,
            family=ContentFamily.JAV,
            code="FC2-3131319",
        )
    )
    assert set(called) == {"fc2club", "fc2hub", "fc2contents", "paipancon", "javdb", "r18dev"}
