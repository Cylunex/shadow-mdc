import asyncio
import time
from collections.abc import Iterable
from dataclasses import dataclass
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
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> str:
        return await self._request_text(
            provider,
            "GET",
            url,
            params=params,
            headers=headers,
            cookies=cookies,
        )

    async def _post_text(
        self,
        provider: str,
        url: str,
        *,
        data: dict[str, str],
    ) -> str:
        return await self._request_text(provider, "POST", url, data=data)

    async def _request_text(
        self,
        provider: str,
        method: str,
        url: str,
        *,
        params: dict[str, str] | None = None,
        data: dict[str, str] | None = None,
        headers: dict[str, str] | None = None,
        cookies: dict[str, str] | None = None,
    ) -> str:
        response: httpx.Response | None = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.request(
                    method,
                    url,
                    params=params,
                    data=data,
                    headers=headers,
                    cookies=cookies,
                )
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
    def __init__(
        self,
        providers: Iterable[Provider],
        *,
        failure_threshold: int = 4,
        cooldown_seconds: float = 300,
        max_concurrent_calls: int = 16,
    ):
        self._providers = {provider.descriptor.id: provider for provider in providers}
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._call_slots = asyncio.Semaphore(max_concurrent_calls)
        self._health: dict[str, _ProviderHealth] = {}

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
        now = time.monotonic()
        active: list[Provider] = []
        failures: list[ProviderFailure] = []
        for provider in eligible:
            health = self._health.get(provider.descriptor.id)
            if health is not None and health.retry_after > now:
                failures.append(
                    ProviderFailure(
                        provider=provider.descriptor.id,
                        reason="cooldown",
                        detail=f"retry_in={max(1, round(health.retry_after - now))}s",
                    )
                )
            else:
                active.append(provider)

        async def invoke(provider: Provider) -> tuple[list[ProviderRecord], ProviderFailure | None]:
            try:
                async with self._call_slots:
                    records = await provider.search(hints)
                self._health.pop(provider.descriptor.id, None)
                return records, None
            except ProviderError as exc:
                self._record_failure(exc)
                return [], ProviderFailure(provider=exc.provider, reason=exc.reason, detail=exc.detail)
            except Exception as exc:
                return [], ProviderFailure(
                    provider=provider.descriptor.id,
                    reason="unexpected",
                    detail=f"{type(exc).__name__}: {exc}",
                )

        results = await asyncio.gather(*(invoke(provider) for provider in active))
        records: list[ProviderRecord] = []
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

    def _record_failure(self, error: ProviderError) -> None:
        if error.reason not in {"blocked", "connect_timeout", "network", "timeout"}:
            return
        health = self._health.setdefault(error.provider, _ProviderHealth())
        health.failures += 1
        if health.failures >= self._failure_threshold:
            health.retry_after = time.monotonic() + self._cooldown_seconds


@dataclass
class _ProviderHealth:
    failures: int = 0
    retry_after: float = 0
