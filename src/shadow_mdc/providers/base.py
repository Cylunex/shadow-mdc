import asyncio
from collections.abc import Iterable
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord


class ProviderError(RuntimeError):
    def __init__(self, provider: str, reason: str, detail: str):
        super().__init__(f"{provider}: {reason}: {detail}")
        self.provider = provider
        self.reason = reason
        self.detail = detail


class ProviderFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    reason: str
    detail: str


class SearchBatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    records: tuple[ProviderRecord, ...]
    failures: tuple[ProviderFailure, ...]


class Provider(Protocol):
    @property
    def descriptor(self) -> ProviderDescriptor: ...

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]: ...


class HttpProvider:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client

    async def _get_text(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> str:
        try:
            response = await self._client.get(url, params=params)
            response.raise_for_status()
        except httpx.TimeoutException as exc:
            raise ProviderError(provider, "timeout", str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(provider, "http", f"status={exc.response.status_code}") from exc
        except httpx.HTTPError as exc:
            raise ProviderError(provider, "network", str(exc)) from exc
        text = response.text
        lowered = text.casefold()
        if "cf-chl-" in lowered or "just a moment" in lowered:
            raise ProviderError(provider, "blocked", "Cloudflare challenge")
        return text


class ProviderRegistry:
    def __init__(self, providers: Iterable[Provider]):
        self._providers = {provider.descriptor.id: provider for provider in providers}

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(
            sorted((provider.descriptor for provider in self._providers.values()), key=lambda item: item.id)
        )

    async def search(self, hints: IdentityHints) -> SearchBatch:
        eligible = [
            provider
            for provider in self._providers.values()
            if provider.descriptor.configured
            and hints.mode in provider.descriptor.query_modes
            and (
                not provider.descriptor.families
                or hints.family in provider.descriptor.families
                or hints.family.value == "unknown"
            )
        ]

        async def invoke(provider: Provider) -> tuple[list[ProviderRecord], ProviderFailure | None]:
            try:
                return await provider.search(hints), None
            except ProviderError as exc:
                return [], ProviderFailure(provider=exc.provider, reason=exc.reason, detail=exc.detail)
            except Exception as exc:
                return [], ProviderFailure(
                    provider=provider.descriptor.id,
                    reason="unexpected",
                    detail=f"{type(exc).__name__}: {exc}",
                )

        results = await asyncio.gather(*(invoke(provider) for provider in eligible))
        records: list[ProviderRecord] = []
        failures: list[ProviderFailure] = []
        seen: set[tuple[str, str]] = set()
        for provider_records, failure in results:
            if failure is not None:
                failures.append(failure)
            for record in provider_records:
                key = (record.provider, record.external_id)
                if key not in seen:
                    seen.add(key)
                    records.append(record)
        return SearchBatch(records=tuple(records), failures=tuple(failures))
