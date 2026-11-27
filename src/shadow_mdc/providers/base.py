import asyncio
import time
from collections.abc import Iterable
from contextlib import AsyncExitStack
from dataclasses import dataclass
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from math import isfinite
from typing import Annotated, Protocol

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from .challenge import challenge_kind
from .impersonate import FALLBACK_STATUSES, ImpersonateError, impersonated_get

ProviderSelection = bool | Annotated[str, Field(max_length=500)]
LOCAL_METADATA_PROVIDER_IDS = ("r18dump",)
_PROVIDER_SELECTION_FLAGS = dict.fromkeys(("true", "1", "yes", "on"), True) | dict.fromkeys(
    ("false", "0", "no", "off"), False,
)


def normalize_provider_selection(selection: ProviderSelection | None) -> ProviderSelection | None:
    if isinstance(selection, str):
        key = selection.strip().casefold()
        if key in _PROVIDER_SELECTION_FLAGS:
            return _PROVIDER_SELECTION_FLAGS[key]
    return selection


class ProviderError(RuntimeError):
    def __init__(
        self, provider: str, reason: str, detail: str, *, retry_after_seconds: float | None = None
    ) -> None:
        super().__init__(f"{provider}: {reason}: {detail}")
        self.provider = provider
        self.reason = reason
        self.detail = detail
        self.retry_after_seconds = retry_after_seconds


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
    # Access denials stop the source; alternate transports are never automatic.
    impersonate_fallback: bool = False

    def __init__(self, client: httpx.AsyncClient, retries: int = 1) -> None:
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
        try:
            return await self._httpx_request_text(
                provider, method, url, params=params, data=data, headers=headers, cookies=cookies
            )
        except ProviderError as exc:
            if method != "GET" or not self.impersonate_fallback or exc.reason != "blocked":
                raise
            return await self._impersonated_text(
                provider, url, exc, params=params, headers=headers, cookies=cookies
            )

    async def _impersonated_text(
        self,
        provider: str,
        url: str,
        original: ProviderError,
        *,
        params: dict[str, str] | None,
        headers: dict[str, str] | None,
        cookies: dict[str, str] | None,
    ) -> str:
        merged_cookies = {
            **{c.name: c.value for c in self._client.cookies.jar if c.value is not None}, **(cookies or {})
        }
        merged_headers = {k: v for k, v in self._client.headers.items() if k.casefold() not in _HOP_HEADERS}
        merged_headers.update(headers or {})
        try:
            response = await impersonated_get(
                url,
                params=params,
                headers=merged_headers,
                cookies=merged_cookies or None,
                timeout=_client_timeout(self._client),
            )
        except ImpersonateError as exc:
            raise ProviderError(provider, "blocked", f"{original.detail}; impersonate: {exc}") from exc
        kind = challenge_kind(response.text, response.url)
        if kind is not None:
            raise ProviderError(
                provider, "blocked", f"{original.detail}; impersonate: {kind} (HTTP {response.status_code})"
            )
        if response.status_code in FALLBACK_STATUSES or not response.ok:
            raise ProviderError(
                provider, "blocked", f"{original.detail}; impersonate: status={response.status_code}"
            )
        return response.text

    async def _httpx_request_text(
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
            except httpx.PoolTimeout as exc:
                # Repeating the request consumes more of the same exhausted pool.
                # This is local pressure, not evidence that the remote source failed.
                raise ProviderError(provider, "pool_timeout", _http_error_detail(exc, attempt + 1)) from exc
            except httpx.TimeoutException as exc:
                if attempt < self._retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                reason = "connect_timeout" if isinstance(exc, httpx.ConnectTimeout) else "timeout"
                raise ProviderError(provider, reason, _http_error_detail(exc, attempt + 1)) from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                retry_after = retry_after_seconds(exc.response.headers.get("Retry-After"))
                if code == 429 or (code == 503 and retry_after is not None):
                    raise ProviderError(
                        provider,
                        "rate_limited",
                        f"status={code}; attempts={attempt + 1}",
                        retry_after_seconds=retry_after,
                    ) from exc
                if code in {403, 429, 503}:
                    # Challenge walls are often 403/503: report them as such and
                    # do not burn retries on a page that will not change.
                    kind = challenge_kind(_safe_text(exc.response), str(exc.response.url))
                    if kind is not None:
                        raise ProviderError(provider, "blocked", f"{kind} (HTTP {code})") from exc
                if attempt < self._retries and code >= 500:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise ProviderError(
                    provider,
                    "blocked" if code in {403, 451} else "http",
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
        kind = challenge_kind(text, str(response.url))
        if kind is not None:
            raise ProviderError(provider, "blocked", kind)
        return text


_HOP_HEADERS = frozenset({"host", "content-length", "accept-encoding", "connection", "user-agent", "cookie"})


def retry_after_seconds(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        delay = float(value)
    except ValueError:
        try:
            deadline = parsedate_to_datetime(value)
            if deadline.tzinfo is None:
                deadline = deadline.replace(tzinfo=UTC)
            delay = (deadline - datetime.now(UTC)).total_seconds()
        except (ValueError, TypeError, OverflowError):
            return None
    return max(0.0, delay) if isfinite(delay) else None


def _client_timeout(client: httpx.AsyncClient) -> float:
    timeout = client.timeout
    values = [v for v in (timeout.read, timeout.connect) if v is not None]
    return float(max(values)) if values else 20.0


def _safe_text(response: httpx.Response) -> str:
    try:
        return response.text
    except (httpx.ResponseNotRead, UnicodeDecodeError):
        return ""


def _http_error_detail(exc: httpx.HTTPError, attempts: int) -> str:
    message = str(exc).strip() or type(exc).__name__
    return f"{type(exc).__name__}: {message}; attempts={attempts}"


@dataclass
class ProviderHealth:
    failures: int = 0
    retry_after: float = 0


class ProviderCooldown:
    """Process-local health and backoff for independent provider channels."""

    def __init__(self, *, failure_threshold: int = 4, cooldown_seconds: float = 300) -> None:
        self._failure_threshold = failure_threshold
        self._cooldown_seconds = cooldown_seconds
        self._health: dict[str, ProviderHealth] = {}

    def health(self, provider: str) -> ProviderHealth | None:
        return self._health.get(provider)

    def remaining(self, provider: str, now: float) -> int | None:
        health = self.health(provider)
        if health is not None and health.retry_after > now:
            return max(1, round(health.retry_after - now))
        return None

    def clear(self, provider: str | None = None) -> int:
        if provider is None:
            count = len(self._health)
            self._health.clear()
            return count
        return int(self._health.pop(provider, None) is not None)

    def record_failure(self, provider: str, reason: str, *, retry_after: float | None = None) -> None:
        if reason not in {"blocked", "connect_timeout", "network", "timeout", "rate_limited"}:
            return
        health = self._health.setdefault(provider, ProviderHealth())
        health.failures += 1
        if reason in {"rate_limited", "blocked"}:
            health.retry_after = time.monotonic() + max(
                0.25, retry_after if retry_after is not None else self._cooldown_seconds
            )
        elif health.failures >= self._failure_threshold:
            health.retry_after = time.monotonic() + self._cooldown_seconds


class ProviderRegistry:
    def __init__(
        self,
        providers: Iterable[Provider],
        *,
        failure_threshold: int = 4,
        cooldown_seconds: float = 300,
        max_concurrent_calls: int = 16,
        max_concurrent_per_provider: int = 2,
        call_timeout_seconds: float = 90,
    ) -> None:
        if call_timeout_seconds <= 0 or not isfinite(call_timeout_seconds):
            raise ValueError("call_timeout_seconds must be finite and positive")
        if max_concurrent_calls < 1 or max_concurrent_per_provider < 1:
            raise ValueError("provider concurrency must be positive")
        self._providers = {provider.descriptor.id: provider for provider in providers}
        self._cooldown = ProviderCooldown(
            failure_threshold=failure_threshold, cooldown_seconds=cooldown_seconds,
        )
        self._call_slots = asyncio.Semaphore(max_concurrent_calls)
        self._provider_slots = {
            key: asyncio.Semaphore(max_concurrent_per_provider) for key in self._providers
        }
        self._call_timeout_seconds = call_timeout_seconds

    def descriptors(self) -> tuple[ProviderDescriptor, ...]:
        return tuple(
            sorted((provider.descriptor for provider in self._providers.values()), key=lambda item: item.id)
        )

    def is_configured(self, provider_id: str) -> bool:
        provider = self._providers.get(provider_id)
        return provider is not None and provider.descriptor.configured

    def select_ids(self, selection: ProviderSelection | None) -> tuple[str, ...] | None:
        """Translate the API selector to a registry allowlist."""

        selection = normalize_provider_selection(selection)
        if selection is None or selection is True:
            return None
        if selection is False:
            return LOCAL_METADATA_PROVIDER_IDS
        selected = tuple(dict.fromkeys(
            part.strip().casefold() for part in selection.split(",") if part.strip()
        ))
        if not selected or set(selected) - self._providers.keys():
            raise ValueError("unknown metadata provider")
        return selected

    async def search(
        self,
        hints: IdentityHints,
        *,
        provider_ids: tuple[str, ...] | None = None,
    ) -> SearchBatch:
        eligible = self._eligible(hints, provider_ids)
        now = time.monotonic()
        return await self._search_eligible(hints, eligible, now)

    def eligible_ids(
        self, hints: IdentityHints, provider_ids: tuple[str, ...] | None = None
    ) -> tuple[str, ...]:
        return tuple(provider.descriptor.id for provider in self._eligible(hints, provider_ids))

    def _eligible(self, hints: IdentityHints, provider_ids: tuple[str, ...] | None) -> list[Provider]:
        allowed = set(provider_ids) if provider_ids is not None else None
        return [
            provider
            for provider in self._providers.values()
            if provider.descriptor.configured
            and (allowed is None or provider.descriptor.id in allowed)
            and hints.mode in provider.descriptor.query_modes
            and (
                not provider.descriptor.families
                or hints.family in provider.descriptor.families
                or hints.family.value == "unknown"
            )
        ]

    async def _search_eligible(
        self, hints: IdentityHints, eligible: list[Provider], now: float
    ) -> SearchBatch:
        active: list[Provider] = []
        failures: list[ProviderFailure] = []
        for provider in eligible:
            remaining = self._cooldown.remaining(provider.descriptor.id, now)
            if remaining is not None:
                failures.append(
                    ProviderFailure(
                        provider=provider.descriptor.id,
                        reason="cooldown",
                        detail=f"retry_in={remaining}s",
                    )
                )
            else:
                active.append(provider)

        async def invoke(provider: Provider) -> tuple[list[ProviderRecord], ProviderFailure | None]:
            started = False
            try:
                async with AsyncExitStack() as slots:
                    # Queuing has its own bounded budget so a slow source can
                    # release a global slot and let a healthy source finish.
                    async with asyncio.timeout(self._call_timeout_seconds * 2):
                        await slots.enter_async_context(self._provider_slots[provider.descriptor.id])
                        await slots.enter_async_context(self._call_slots)
                    # A preceding call may have opened the circuit while we queued.
                    remaining = self._cooldown.remaining(provider.descriptor.id, time.monotonic())
                    if remaining is not None:
                        return [], ProviderFailure(
                            provider=provider.descriptor.id,
                            reason="cooldown",
                            detail=f"retry_in={remaining}s",
                        )
                    started = True
                    async with asyncio.timeout(self._call_timeout_seconds):
                        records = await provider.search(hints)
                self._cooldown.clear(provider.descriptor.id)
                return records, None
            except ProviderError as exc:
                self._record_failure(exc)
                return [], ProviderFailure(provider=exc.provider, reason=exc.reason, detail=exc.detail)
            except TimeoutError:
                if not started:
                    return [], ProviderFailure(
                        provider=provider.descriptor.id, reason="capacity_timeout",
                        detail=f"queue deadline={self._call_timeout_seconds * 2:g}s",
                    )
                error = ProviderError(
                    provider.descriptor.id, "timeout", f"search deadline={self._call_timeout_seconds:g}s"
                )
                self._record_failure(error)
                return [], ProviderFailure(provider=error.provider, reason=error.reason, detail=error.detail)
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

    def health_snapshot(self) -> list[dict[str, object]]:
        now = time.monotonic()
        rows: list[dict[str, object]] = []
        for provider in sorted(self._providers.values(), key=lambda item: item.descriptor.id):
            health = self._cooldown.health(provider.descriptor.id)
            retry_in = self._cooldown.remaining(provider.descriptor.id, now)
            rows.append(
                {
                    "provider": provider.descriptor.id,
                    "configured": provider.descriptor.configured,
                    "failures": health.failures if health is not None else 0,
                    "cooldown": retry_in is not None,
                    "retry_in_seconds": retry_in,
                }
            )
        return rows

    def clear_cooldown(self, provider_id: str | None = None) -> int:
        return self._cooldown.clear(provider_id)

    def _record_failure(self, error: ProviderError) -> None:
        self._cooldown.record_failure(error.provider, error.reason, retry_after=error.retry_after_seconds)
