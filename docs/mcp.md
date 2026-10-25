# Shadow MDC MCP

Two layers:

1. **HTTP API** `/api/mcp/v1/*` inside shadow-mdc. It is enabled only when `SHADOW_MDC_MCP_TOKEN` is set; if it is unset, every call returns 503. Every request must send `Authorization: Bearer <token>`, compared in constant time, or it gets 401. The web UI still uses OIDC; this token covers only `/api/mcp/v1`.
2. **MCP server** `mcp_server/server.py` (official `mcp` Python SDK). It is a thin client over the API and supports stdio and streamable HTTP.

## API

| Method | Path | Notes |
|---|---|---|
| GET | `/api/mcp/v1/health` | auth check + 115 status |
| GET | `/api/mcp/v1/library/{code}` | catalog/library entry, STRM, subtitles, running job |
| GET | `/api/mcp/v1/metadata/{code}?providers=javdb,javbus` | r18 dump + providers |
| POST | `/api/mcp/v1/offline` `{code, magnet?, dry_run?}` | 番号 → 115 offline. action = exists / running / planned / submitted / hunting / error |
| GET | `/api/mcp/v1/jobs?status=&limit=` | offline jobs |
| GET | `/api/mcp/v1/jobs/{job_id}` | stage downloading → organizing → in_library |
| POST | `/api/mcp/v1/subscribe` `{code}` or `{actor}` | want list / actor 盯磁 |
| POST | `/api/mcp/v1/rescrape` `{code}` | respects locks (409) and quota (429) |

How offline works:

- The code is normalized first (`ssis123` / full-width characters → `SSIS-123`).
- If the code is already in the library, or a job for it is already running, that entry is returned.
- Otherwise the best magnet is chosen: code match > 中字 > HD > size. The search order is stored magnets → javdb → sukebei.
- After the 115 job finishes, the existing poller organizes the files, scrapes metadata, writes the STRM/NFO, fetches subtitles and notifies Emby.
- If no magnet is found, the code goes to the want list (`hunting`).
- `dry_run=true` returns only the plan and writes nothing.

## MCP tools

`library_lookup`, `metadata_lookup`, `offline_add` (defaults to `dry_run=true`), `job_status`, `list_jobs`, `subscribe`, `rescrape`.

## Run

```bash
pip install -r mcp_server/requirements.txt     # or: pip install '.[mcp]' uvicorn
export SHADOW_MDC_URL=http://<nas-lan-or-tailscale-ip>:8700   # or via the /mdc/ proxy is NOT supported (OIDC)
export SHADOW_MDC_MCP_TOKEN_FILE=/path/to/mcp-token   # or SHADOW_MDC_MCP_TOKEN=...
python -m mcp_server.server                                   # stdio
python -m mcp_server.server --transport http --host 0.0.0.0 --port 8701   # http://host:8701/mcp
```

In HTTP mode the MCP endpoint also requires `Authorization: Bearer <token>`. That token is `SHADOW_MDC_MCP_HTTP_TOKEN`, or `SHADOW_MDC_MCP_TOKEN` if the first is unset. Keep the endpoint on the LAN or Tailscale only.

## Claude Desktop / Cursor config (stdio)

```json
{
  "mcpServers": {
    "shadow-mdc": {
      "command": "python",
      "args": ["-m", "mcp_server.server"],
      "cwd": "/path/to/shadow-mdc",
      "env": {
        "SHADOW_MDC_URL": "http://192.168.x.x:8700",
        "SHADOW_MDC_MCP_TOKEN": "<your-token>"
      }
    }
  }
}
```

## Cursor (streamable HTTP, server running on NAS)

```json
{
  "mcpServers": {
    "shadow-mdc": {
      "url": "http://<nas-tailscale-ip>:8701/mcp",
      "headers": { "Authorization": "Bearer <your-token>" }
    }
  }
}
```
