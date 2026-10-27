"""Reverse-proxy tube swipefeed APIs under ``/api/swipe/*``.

The Node sidecar (``services/swipefeed``) owns listing, resolve, circuit breaker,
and media byte proxy (HLS/stream/png-hls). FastAPI only forwards so the SPA and
nginx ``/mdc/`` OIDC edge stay one product — no separate public swipefeed app.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator, Mapping
from typing import Any
from urllib.parse import urljoin

import httpx
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

from .config import Settings

logger = logging.getLogger(__name__)

# Hop-by-hop / framing headers we must not blindly copy.
_HOP_BY_HOP = {
    "connection",
    "keep-alive",
    "proxy-authenticate",
    "proxy-authorization",
    "te",
    "trailers",
    "transfer-encoding",
    "upgrade",
    "host",
    "content-length",
}


def create_swipe_proxy_router(settings: Settings) -> APIRouter:
    router = APIRouter(prefix="/api/swipe", tags=["swipe"])

    @router.get("/health")
    async def swipe_health() -> JSONResponse:
        upstream = (settings.swipe_upstream_url or "").rstrip("/")
        if not upstream:
            return JSONResponse(
                {"ok": False, "upstream": None, "error": "swipe_upstream_url unset"},
                status_code=503,
            )
        try:
            async with httpx.AsyncClient(timeout=5.0) as client:
                # Sidecar may not expose /health; probe a cheap JSON route.
                resp = await client.get(f"{upstream}/api/sites")
            return JSONResponse(
                {
                    "ok": resp.status_code < 500,
                    "upstream": upstream,
                    "status": resp.status_code,
                },
                status_code=200 if resp.status_code < 500 else 503,
            )
        except Exception as exc:  # noqa: BLE001 — surface probe failure
            return JSONResponse(
                {"ok": False, "upstream": upstream, "error": str(exc)},
                status_code=503,
            )

    @router.api_route(
        "/{path:path}",
        methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    )
    async def proxy(path: str, request: Request) -> Response:
        upstream = (settings.swipe_upstream_url or "").rstrip("/")
        if not upstream:
            return JSONResponse(
                {"error": "tube swipe sidecar not configured (SHADOW_MDC_SWIPE_UPSTREAM_URL)"},
                status_code=503,
            )

        # Map /api/swipe/<rest> → sidecar /api/<rest>
        # Also accept /api/swipe/api/<rest> (legacy SWIPEFEED_BASE style) → /api/<rest>
        rest = path.lstrip("/")
        if rest.startswith("api/"):
            target_path = f"/{rest}"
        else:
            target_path = f"/api/{rest}" if rest else "/api/sites"
        query = request.url.query
        url = urljoin(upstream + "/", target_path.lstrip("/"))
        if query:
            url = f"{url}?{query}"

        headers = _forward_request_headers(request.headers)
        body = await request.body()
        timeout = httpx.Timeout(settings.swipe_proxy_timeout_seconds, connect=10.0)

        client = httpx.AsyncClient(timeout=timeout, follow_redirects=False)
        try:
            req = client.build_request(
                request.method,
                url,
                headers=headers,
                content=body if body else None,
            )
            upstream_resp = await client.send(req, stream=True)
        except httpx.RequestError as exc:
            await client.aclose()
            logger.warning("swipe upstream error %s %s: %s", request.method, url, exc)
            return JSONResponse({"error": f"swipe upstream unreachable: {exc}"}, status_code=502)

        resp_headers = _forward_response_headers(upstream_resp.headers)
        media_type = upstream_resp.headers.get("content-type")

        async def stream() -> AsyncIterator[bytes]:
            try:
                async for chunk in upstream_resp.aiter_raw():
                    yield chunk
            finally:
                await upstream_resp.aclose()
                await client.aclose()

        return StreamingResponse(
            stream(),
            status_code=upstream_resp.status_code,
            headers=resp_headers,
            media_type=media_type,
        )

    return router


def _forward_request_headers(headers: Mapping[str, str]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk in _HOP_BY_HOP or lk == "cookie":
            # Cookie stays on the mdc OIDC edge; sidecar does not need browser cookies.
            continue
        out[key] = value
    # Identify proxy for logs
    out.setdefault("X-Forwarded-By", "shadow-mdc-swipe-proxy")
    return out


def _forward_response_headers(headers: Mapping[str, Any]) -> dict[str, str]:
    out: dict[str, str] = {}
    for key, value in headers.items():
        lk = key.lower()
        if lk in _HOP_BY_HOP:
            continue
        out[key] = value
    return out
