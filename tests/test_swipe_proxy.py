"""Tube swipe FastAPI reverse-proxy (/api/swipe/*)."""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.config import Settings
from shadow_mdc.swipe_proxy import create_swipe_proxy_router


def test_swipe_health_unset_upstream() -> None:
    # Settings may coerce empty; force blank via model if needed
    router = create_swipe_proxy_router(Settings.model_construct(swipe_upstream_url="", swipe_proxy_timeout_seconds=30.0))
    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(router)
    with TestClient(app) as client:
        resp = client.get("/api/swipe/health")
        assert resp.status_code == 503
        assert resp.json().get("ok") is False


def test_swipe_proxy_maps_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    seen: list[str] = []
    requests: list[httpx.Request] = []
    clients: list[object] = []

    class FakeStreamResp:
        status_code = 200
        headers = httpx.Headers({"content-type": "application/json", "x-test": "1"})

        async def aiter_raw(self):
            yield b'{"ok":true}'

        async def aclose(self):
            return None

    class FakeClient:
        def __init__(self, *args, **kwargs):
            clients.append(self)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            await self.aclose()

        async def send(self, req, stream=False):
            requests.append(req)
            seen.append(str(req.url))
            return FakeStreamResp()

        async def aclose(self):
            return None

    monkeypatch.setattr(httpx, "AsyncClient", FakeClient)

    from fastapi import FastAPI

    app = FastAPI()
    app.include_router(
        create_swipe_proxy_router(
            Settings.model_construct(
                swipe_upstream_url="http://127.0.0.1:8791",
                swipe_proxy_timeout_seconds=30.0,
            )
        )
    )
    with TestClient(app) as client:
        resp = client.get("/api/swipe/feed?limit=2")
        assert resp.status_code == 200
        assert resp.json() == {"ok": True}
        assert seen and seen[0].startswith("http://127.0.0.1:8791/api/feed")

        seen.clear()
        resp = client.get("/api/swipe/api/hls?u=http://example.invalid/a.m3u8", headers={"Authorization": "Bearer secret", "Cookie": "user=secret"})
        assert resp.status_code == 200
        assert "/api/hls" in seen[0]
        assert client.get("/api/swipe/health").json()["ok"] is True
        assert seen[-1].endswith("/api/sites")
        assert len(clients) == 1
        assert "authorization" not in requests[-1].headers and "cookie" not in requests[-1].headers


def test_swipe_entry_mentions_tube(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from shadow_mdc.api import app

    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'swipe.db'}")
    with TestClient(app) as client:
        body = client.get("/swipe/").text
        assert "刷流" in body
