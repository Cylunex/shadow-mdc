"""Optional Emby/Jellyfin library refresh after organize."""

from __future__ import annotations

import json
from pathlib import Path
from dataclasses import dataclass

import httpx
from pydantic import BaseModel, ConfigDict, Field


class MediaServerSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    enabled: bool = False
    kind: str = "jellyfin"  # jellyfin | emby
    base_url: str | None = None
    api_key: str | None = None
    verify_nfo_fields: bool = False
    # Optional Emby/Jellyfin deep link; {query} replaced with code/title
    deep_link_template: str | None = None


class RefreshResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    attempted: bool
    ok: bool
    detail: str
    nfo_check: dict[str, object] = Field(default_factory=dict)


@dataclass(slots=True)
class MediaServerConnector:
    settings: MediaServerSettings
    client: httpx.AsyncClient

    async def refresh_path(self, path: str) -> RefreshResult:
        if not self.settings.enabled:
            return RefreshResult(attempted=False, ok=True, detail="disabled")
        if not self.settings.base_url or not self.settings.api_key:
            return RefreshResult(attempted=False, ok=False, detail="missing base_url or api_key")
        base = self.settings.base_url.rstrip("/")
        headers = {"X-Emby-Token": self.settings.api_key}
        # Jellyfin/Emby share Items/Refresh semantics for path-based refresh via library scan endpoint.
        try:
            response = await self.client.post(
                f"{base}/Library/Media/Updated",
                headers=headers,
                json={"Updates": [{"Path": path, "UpdateType": "Modified"}]},
                timeout=20.0,
            )
            if response.status_code >= 400:
                # Fallback: trigger a full library refresh stub.
                fallback = await self.client.post(
                    f"{base}/Library/Refresh",
                    headers=headers,
                    timeout=20.0,
                )
                fallback.raise_for_status()
                detail = f"path refresh HTTP {response.status_code}; triggered Library/Refresh"
            else:
                detail = "path refresh accepted"
            nfo_check: dict[str, object] = {}
            if self.settings.verify_nfo_fields:
                nfo_check = await self._stub_nfo_field_check(path, headers, base)
            return RefreshResult(attempted=True, ok=True, detail=detail, nfo_check=nfo_check)
        except (httpx.HTTPError, ValueError) as exc:
            return RefreshResult(attempted=True, ok=False, detail=f"{type(exc).__name__}: {exc}")

    async def _stub_nfo_field_check(
        self,
        path: str,
        headers: dict[str, str],
        base: str,
    ) -> dict[str, object]:
        """Optional stub: query Items by path and report whether title/overview exist."""

        try:
            response = await self.client.get(
                f"{base}/Items",
                headers=headers,
                params={"Path": path, "Recursive": "true", "Fields": "Overview,Path"},
                timeout=20.0,
            )
            if response.status_code >= 400:
                return {"status": "unavailable", "http_status": response.status_code}
            payload = response.json()
            items = payload.get("Items") if isinstance(payload, dict) else None
            if not isinstance(items, list) or not items:
                return {"status": "not_found"}
            item = items[0] if isinstance(items[0], dict) else {}
            return {
                "status": "ok",
                "has_name": bool(item.get("Name")),
                "has_overview": bool(item.get("Overview")),
                "item_id": item.get("Id"),
            }
        except (httpx.HTTPError, ValueError, TypeError) as exc:
            return {"status": "error", "detail": f"{type(exc).__name__}: {exc}"}



class MediaServerStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> MediaServerSettings:
        if not self._path.is_file():
            return MediaServerSettings()
        try:
            return MediaServerSettings.model_validate_json(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return MediaServerSettings()

    def save(self, settings: MediaServerSettings) -> MediaServerSettings:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(settings.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        return settings


async def refresh_media_server(settings: MediaServerSettings, client: httpx.AsyncClient) -> str:
    connector = MediaServerConnector(settings=settings, client=client)
    # Full library refresh via empty path fallback.
    if not settings.enabled:
        return "disabled"
    if not settings.base_url or not settings.api_key:
        return "missing base_url or api_key"
    base = settings.base_url.rstrip("/")
    headers = {"X-Emby-Token": settings.api_key}
    response = await client.post(f"{base}/Library/Refresh", headers=headers, timeout=20.0)
    response.raise_for_status()
    return f"{settings.kind}:Library/Refresh accepted"


def build_media_server_deep_link(settings: MediaServerSettings, query: str) -> str | None:
    """Build an Emby/Jellyfin search deep-link URL when configured."""

    q = query.strip()
    if not q:
        return None
    template = settings.deep_link_template
    if template:
        return template.replace("{query}", q).replace("{code}", q)
    if not settings.base_url:
        return None
    base = settings.base_url.rstrip("/")
    from urllib.parse import quote
    encoded = quote(q)
    if settings.kind == "emby":
        return f"{base}/web/index.html#!/search?search={encoded}"
    return f"{base}/web/index.html#!/search.html?query={encoded}"
