"""Browser-impersonating HTTP transport (curl_cffi) used as an anti-bot fallback.

Some sources reject httpx purely on its TLS/HTTP2 fingerprint. curl_cffi
replays a real Chrome handshake, so a request that httpx gets 403/451/503 for
is retried through it once. IP/geo blocks are *not* fixed by this; callers still
run :func:`challenge_kind` on the result and fail with ``blocked``.
"""

from __future__ import annotations

from dataclasses import dataclass

from curl_cffi import requests as curl_requests
from curl_cffi.requests.exceptions import RequestException

DEFAULT_IMPERSONATE = "chrome"
# Statuses worth retrying with a browser fingerprint.
FALLBACK_STATUSES: frozenset[int] = frozenset({403, 429, 451, 503})


_default_proxy: str | None = None


def set_default_proxy(proxy_url: str | None) -> None:
    """Route impersonated requests through the same proxy as the httpx clients."""

    global _default_proxy
    _default_proxy = proxy_url or None


class ImpersonateError(RuntimeError):
    """Transport-level failure (DNS, connect, timeout) in the curl_cffi path."""


@dataclass(frozen=True)
class ImpersonatedResponse:
    status_code: int
    url: str
    text: str

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 400


async def impersonated_get(
    url: str,
    *,
    params: dict[str, str] | None = None,
    headers: dict[str, str] | None = None,
    cookies: dict[str, str] | None = None,
    timeout: float = 20.0,
    impersonate: str = DEFAULT_IMPERSONATE,
    proxy: str | None = None,
) -> ImpersonatedResponse:
    """GET ``url`` with a browser TLS fingerprint. Never raises on HTTP status."""

    # Let curl_cffi supply the UA matching the impersonated browser.
    clean_headers = {k: v for k, v in (headers or {}).items() if k.casefold() != "user-agent"}
    try:
        async with curl_requests.AsyncSession(impersonate=impersonate, proxy=proxy or _default_proxy) as session:  # type: ignore[arg-type]
            response = await session.get(
                url,
                params=params,
                headers=clean_headers or None,
                cookies=cookies,
                timeout=timeout,
                allow_redirects=True,
            )
    except RequestException as exc:
        raise ImpersonateError(f"{type(exc).__name__}: {exc}") from exc
    return ImpersonatedResponse(status_code=int(response.status_code), url=str(response.url), text=response.text)
