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
    def __init__(self, client: httpx.AsyncClient, retries: int = 1):
        self._client = client
        self._retries = retries

    async def _get_text(
        self,
        provider: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
    ) -> str:
        response: httpx.Response | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.get(url, params=params)
                response.raise_for_status()
                break
            except httpx.TimeoutException as exc:
                if attempt < self._retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                reason = "connect_timeout" if isinstance(exc, httpx.ConnectTimeout) else "timeout"
                raise ProviderError(provider, reason, _http_error_detail(exc, attempt + 1)) from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if attempt < self._retries and (code == 429 or code >= 500):
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise ProviderError(
                    provider,
                    "blocked" if code in {403, 429} else "http",
                    f"status={code}; attempts={attempt + 1}",
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self._retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise ProviderError(provider, "network", _http_error_detail(exc, attempt + 1)) from exc
        if response is None:
            raise ProviderError(provider, "network", "request did not produce a response")
        text = response.text
        lowered = text.casefold()
        if "cf-chl-" in lowered or "just a moment" in lowered:
            raise ProviderError(provider, "blocked", "Cloudflare challenge")
        return text


def _http_error_detail(exc: httpx.HTTPError, attempts: int) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {message}; attempts={attempts}"


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
