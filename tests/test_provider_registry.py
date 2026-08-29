from dataclasses import dataclass

import pytest

from shadow_mdc.domain import IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderError, ProviderRegistry


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
