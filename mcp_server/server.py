"""Shadow MDC MCP server.

Thin wrapper over the shadow-mdc ``/api/mcp/v1`` HTTP API.

Env:
  SHADOW_MDC_URL            base URL of shadow-mdc (default http://127.0.0.1:8700)
  SHADOW_MDC_MCP_TOKEN      Bearer token for /api/mcp/v1 (or SHADOW_MDC_MCP_TOKEN_FILE)
  SHADOW_MDC_MCP_HTTP_TOKEN optional Bearer token required from MCP clients in HTTP mode
                            (defaults to SHADOW_MDC_MCP_TOKEN)

Run:
  python -m mcp_server.server                       # stdio
  python -m mcp_server.server --transport http --host 0.0.0.0 --port 8701   # streamable HTTP at /mcp
"""

from __future__ import annotations

import argparse
import hmac
import os
from pathlib import Path
from typing import Any

import httpx
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings


def _token() -> str:
    token = os.environ.get("SHADOW_MDC_MCP_TOKEN", "").strip()
    if not token and (path := os.environ.get("SHADOW_MDC_MCP_TOKEN_FILE")):
        token = Path(path).read_text(encoding="utf-8").strip()
    if not token:
        raise SystemExit("SHADOW_MDC_MCP_TOKEN (or SHADOW_MDC_MCP_TOKEN_FILE) is required")
    return token


BASE_URL = os.environ.get("SHADOW_MDC_URL", "http://127.0.0.1:8700").rstrip("/")
API = f"{BASE_URL}/api/mcp/v1"

mcp = FastMCP(
    "shadow-mdc",
    instructions=(
        "Shadow MDC media library. Use offline_add with a JAV code (番号) to put it into the 115 "
        "library via offline download; ALWAYS call it with dry_run=true first and confirm with the "
        "user before a real submit. Use job_status to follow progress."
    ),
)
_client: httpx.AsyncClient | None = None


def _http() -> httpx.AsyncClient:
    global _client
    if _client is None:
        _client = httpx.AsyncClient(
            base_url=API, headers={"Authorization": f"Bearer {_token()}"}, timeout=120.0
        )
    return _client


async def _call(method: str, path: str, **kwargs: Any) -> Any:
    response = await _http().request(method, path, **kwargs)
    try:
        body = response.json()
    except ValueError:
        body = {"raw": response.text[:2000]}
    if response.status_code >= 400:
        return {"error": True, "status": response.status_code, "detail": body}
    return body


@mcp.tool()
async def library_lookup(code: str) -> Any:
    """Check whether a 番号 (e.g. SSIS-123) is in the catalog / library (STRM, subtitle, running job)."""
    return await _call("GET", f"/library/{code}")


@mcp.tool()
async def metadata_lookup(code: str, providers: str | None = None) -> Any:
    """Look up metadata for a 番号 (title, actors, date, cover) from r18 dump / providers.

    providers: optional comma list, e.g. "javdb,javbus".
    """
    params = {"providers": providers} if providers else None
    return await _call("GET", f"/metadata/{code}", params=params)


@mcp.tool()
async def offline_add(code: str, magnet: str | None = None, dry_run: bool = True) -> Any:
    """Add a 番号 to the library via 115 offline download.

    Returns existing entry if already in library / running. Otherwise picks the best magnet
    (中字 > HD > size) and submits; on completion it is organized, scraped, STRM written and
    subtitles fetched. dry_run=true (default) only returns the plan. magnet overrides the pick.
    """
    payload: dict[str, Any] = {"code": code, "dry_run": dry_run}
    if magnet:
        payload["magnet"] = magnet
    return await _call("POST", "/offline", json=payload)


@mcp.tool()
async def job_status(job_id: str) -> Any:
    """Get one offline job (stage: downloading / organizing / in_library, subtitle, error)."""
    return await _call("GET", f"/jobs/{job_id}")


@mcp.tool()
async def list_jobs(status: str | None = None, limit: int = 20) -> Any:
    """List recent offline jobs. status: running|done|failed (optional)."""
    params: dict[str, Any] = {"limit": limit}
    if status:
        params["status"] = status
    return await _call("GET", "/jobs", params=params)


@mcp.tool()
async def subscribe(code: str | None = None, actor: str | None = None) -> Any:
    """Subscribe a 番号 (want list, auto offline when a magnet appears) or an actor (盯磁 new releases)."""
    return await _call("POST", "/subscribe", json={"code": code, "actor": actor})


@mcp.tool()
async def rescrape(code: str) -> Any:
    """Refresh metadata for a catalog 番号 (respects field locks and scrape quota)."""
    return await _call("POST", "/rescrape", json={"code": code})


class _BearerGate:
    def __init__(self, app: Any, token: str) -> None:
        self.app, self.expected = app, f"Bearer {token}".encode()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            got = dict(scope.get("headers") or []).get(b"authorization", b"")
            if not hmac.compare_digest(got, self.expected):
                await send(
                    {
                        "type": "http.response.start",
                        "status": 401,
                        "headers": [(b"content-type", b"application/json")],
                    }
                )
                await send({"type": "http.response.body", "body": b'{"detail":"unauthorized"}'})
                return
        await self.app(scope, receive, send)


def main() -> None:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8701)
    args = parser.parse_args()
    _token()  # fail fast
    if args.transport == "stdio":
        mcp.run("stdio")
        return
    import uvicorn

    mcp.settings.host, mcp.settings.port = args.host, args.port
    # Bearer gate below is the protection; Host check would break LAN/Tailscale access.
    mcp.settings.transport_security = TransportSecuritySettings(enable_dns_rebinding_protection=False)
    gate_token = os.environ.get("SHADOW_MDC_MCP_HTTP_TOKEN", "").strip() or _token()
    uvicorn.run(_BearerGate(mcp.streamable_http_app(), gate_token), host=args.host, port=args.port)


if __name__ == "__main__":
    main()
