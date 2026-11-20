"""Bounded HTTP fetching for untrusted public catalogs and playback checks."""

import asyncio
import ipaddress
import socket
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .playback import validate_m3u8_playlist_text


class TvBoxError(ValueError):
    pass


@dataclass(frozen=True, slots=True)
class Download:
    url: str
    mime: str
    body: bytes


def http_url(value: str, *, allow_http: bool) -> httpx.URL:
    try:
        url = httpx.URL(value)
    except httpx.InvalidURL as exc:
        raise TvBoxError("地址格式无效") from exc
    if (
        not url.host
        or url.userinfo
        or url.scheme not in ("https", "http")
        or (url.scheme == "http" and not allow_http)
        or url.host.lower().rstrip(".").endswith((".localhost", ".local"))
        or url.host.lower().rstrip(".") == "localhost"
    ):
        raise TvBoxError("需要允许访问的 HTTPS 地址")
    return url.copy_with(fragment=None)


async def _public_address(url: httpx.URL) -> str:
    rows = await asyncio.get_running_loop().getaddrinfo(
        url.raw_host.decode(),
        url.port or (443 if url.scheme == "https" else 80),
        type=socket.SOCK_STREAM,
    )
    addresses = [ipaddress.ip_address(row[4][0]) for row in rows]
    if not addresses or any(not ip.is_global or ip.is_multicast for ip in addresses):
        raise TvBoxError("地址解析到了非公网目标")
    return str(addresses[0])


async def download(
    value: str,
    *,
    allow_http: bool,
    max_bytes: int = 2 * 1024 * 1024,
    headers: dict[str, str] | None = None,
    prefix_only: bool = False,
) -> Download:
    try:
        async with (
            asyncio.timeout(9),
            httpx.AsyncClient(
                trust_env=False, timeout=4, limits=httpx.Limits(max_keepalive_connections=0)
            ) as client,
        ):
            url = http_url(value, allow_http=allow_http)
            for _ in range(4):
                ip = await _public_address(url)
                authority = url.netloc.decode()
                server_name = url.raw_host.decode()
                async with client.stream(
                    "GET",
                    url.copy_with(host=ip),
                    headers={"Accept-Encoding": "identity", **(headers or {}), "Host": authority},
                    extensions={"sni_hostname": server_name},
                    follow_redirects=False,
                ) as response:
                    if response.is_redirect:
                        location = response.headers.get("location")
                        if not location:
                            raise TvBoxError("上游重定向缺少目标")
                        next_url = http_url(urljoin(str(url), location), allow_http=allow_http)
                        if url.scheme == "https" and next_url.scheme != "https":
                            raise TvBoxError("上游重定向降低了连接安全性")
                        url = next_url
                        continue
                    response.raise_for_status()
                    encoding = response.headers.get("content-encoding", "identity").lower()
                    if encoding != "identity":
                        raise TvBoxError("上游返回不支持的压缩方式")
                    mime = response.headers.get("content-type", "").split(";", 1)[0].lower()
                    body = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=16384):
                        if prefix_only:
                            body.extend(chunk[: max_bytes - len(body)])
                            if len(body) >= max_bytes:
                                break
                        elif len(body) + len(chunk) > max_bytes:
                            raise TvBoxError("上游响应超过大小限制")
                        else:
                            body.extend(chunk)
                    return Download(str(url), mime, bytes(body))
    except httpx.HTTPStatusError as exc:
        raise TvBoxError(f"上游返回 HTTP {exc.response.status_code}") from exc
    except (OSError, TimeoutError, httpx.HTTPError) as exc:
        raise TvBoxError("上游连接失败或超时") from exc
    raise TvBoxError("上游重定向过多")


async def playback_mime(url: str, *, allow_http: bool, headers: dict[str, str]) -> str:
    response = await download(
        url,
        allow_http=allow_http,
        max_bytes=256 * 1024,
        prefix_only=True,
        headers={**headers, "Range": "bytes=0-262143", "Accept-Encoding": "identity"},
    )
    text = response.body.decode("utf-8", errors="replace")
    if validate_m3u8_playlist_text(text) is None:
        return "application/vnd.apple.mpegurl"
    if len(response.body) > 12 and response.body[4:8] == b"ftyp":
        return "video/mp4"
    raise TvBoxError("线路未返回可确认的 HLS 或 MP4")
