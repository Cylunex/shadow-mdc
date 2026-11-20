import asyncio
from pathlib import Path

import httpx
import pytest

from shadow_mdc.services import tvbox_http
from shadow_mdc.services.tvbox import tvbox_proxy_routes
from shadow_mdc.services.tvbox_http import TvBoxError


async def test_proxy_connects_to_pinned_ip_and_preserves_origin_headers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    traffic: list[bytes] = []

    async def proxy(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            traffic.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()
            traffic.append(await reader.readuntil(b"\r\n\r\n"))
            writer.write(
                b"HTTP/1.1 206 Partial Content\r\n"
                b"Content-Range: bytes 0-2/12\r\nContent-Length: 3\r\n\r\nabc"
            )
            await writer.drain()
        finally:
            writer.close()
            await writer.wait_closed()

    async def validated_ip(url: httpx.URL) -> str:
        return "93.184.216.34" if url.host == "example.com" else "10.0.0.1"

    monkeypatch.setattr(tvbox_http, "_public_address", validated_ip)
    async with await asyncio.start_server(proxy, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        transport = httpx.URL(f"http://127.0.0.1:{port}")
        async with tvbox_http.open_remote(
            "http://example.com/segment?q=1",
            allow_http=True,
            headers={"Range": "bytes=0-2"},
            proxy=transport,
        ) as (url, response):
            assert str(url) == "http://example.com/segment?q=1"
            assert response.status_code == 206 and await response.aread() == b"abc"
        assert traffic[0].startswith(b"CONNECT 93.184.216.34:80 HTTP/1.1\r\n")
        assert traffic[1].startswith(b"GET /segment?q=1 HTTP/1.1\r\n")
        assert b"host: example.com\r\n" in traffic[1].lower()
        assert b"range: bytes=0-2\r\n" in traffic[1].lower()
        with pytest.raises(TvBoxError, match="上游连接失败或超时"):
            await tvbox_http.download(
                "http://other.example.com/segment", allow_http=True, proxy=transport,
            )
        assert len(traffic) == 2


def test_proxy_configuration_limits_routing_to_selected_site_ids(tmp_path: Path) -> None:
    path = tmp_path / "tvbox-proxy.local.json"
    proxy = "http://proxy.example.com:8080"
    assert tvbox_proxy_routes(path, proxy) == {}
    site_id = "a" * 32
    path.write_text(f'{{"site_ids": ["{site_id}"]}}')
    assert tvbox_proxy_routes(path, proxy) == {site_id: httpx.URL(proxy)}
    with pytest.raises(ValueError, match="TVBox 代理站点配置无效"):
        tvbox_proxy_routes(path, "https://user:secret@proxy.example.com")
