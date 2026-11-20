"""Bounded HTTP fetching for untrusted public catalogs and playback checks."""

import asyncio
import ipaddress
import socket
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urljoin

import httpx

from .playback import validate_m3u8_playlist_text
from .tvbox_proxy import open_proxy


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


@asynccontextmanager
async def open_remote(
    value: str,
    *,
    allow_http: bool,
    headers: dict[str, str] | None = None,
    proxy: httpx.URL | None = None,
) -> AsyncIterator[tuple[httpx.URL, httpx.Response]]:
    async with httpx.AsyncClient(
        trust_env=False,
        timeout=4,
        limits=httpx.Limits(max_keepalive_connections=0),
    ) as client:
        url = http_url(value, allow_http=allow_http)
        for _ in range(4):
            async with AsyncExitStack() as stack:
                async with asyncio.timeout(7):
                    ip = await _public_address(url)
                    request_headers = {
                        "Accept-Encoding": "identity", **(headers or {}), "Host": url.netloc.decode(),
                    }
                    request = client.build_request(
                        "GET",
                        url.copy_with(host=ip),
                        headers=request_headers,
                        extensions={"sni_hostname": url.raw_host.decode()},
                    )
                    request.headers.pop("cookie", None)
                    if proxy is None:
                        response = await client.send(request, stream=True)
                        stack.push_async_callback(response.aclose)
                    else:
                        response = await stack.enter_async_context(open_proxy(proxy, request))
                if response.is_redirect:
                    location = response.headers.get("location")
                    if not location:
                        raise TvBoxError("上游重定向缺少目标")
                    next_url = http_url(urljoin(str(url), location), allow_http=allow_http)
                    if url.scheme == "https" and next_url.scheme != "https":
                        raise TvBoxError("上游重定向降低了连接安全性")
                    url = next_url
                    continue
                if response.headers.get("content-encoding", "identity").lower() != "identity":
                    raise TvBoxError("上游返回不支持的压缩方式")
                yield url, response
                return
    raise TvBoxError("上游重定向过多")


async def download(
    value: str,
    *,
    allow_http: bool,
    max_bytes: int = 2 * 1024 * 1024,
    headers: dict[str, str] | None = None,
    prefix_only: bool = False,
    proxy: httpx.URL | None = None,
) -> Download:
    try:
        async with (
            asyncio.timeout(9),
            open_remote(value, allow_http=allow_http, headers=headers, proxy=proxy) as (url, response),
        ):
            response.raise_for_status()
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


async def playback_mime(
    url: str, *, allow_http: bool, headers: dict[str, str], proxy: httpx.URL | None = None,
) -> str:
    response = await download(
        url,
        allow_http=allow_http,
        max_bytes=256 * 1024,
        prefix_only=True,
        headers=headers,
        proxy=proxy,
    )
    text = response.body.decode("utf-8-sig", errors="replace")
    if validate_m3u8_playlist_text(text) is None:
        try:
            async with asyncio.timeout(8):
                await _check_hls_media(
                    response.url, text, allow_http=allow_http, headers=headers, proxy=proxy,
                )
        except TimeoutError as exc:
            raise TvBoxError("HLS 分片读取超时") from exc
        return "application/vnd.apple.mpegurl"
    if len(response.body) > 12 and response.body[4:8] == b"ftyp":
        return "video/mp4"
    raise TvBoxError("线路未返回可确认的 HLS 或 MP4")


async def _check_hls_media(
    url: str, text: str, *, allow_http: bool, headers: dict[str, str], proxy: httpx.URL | None,
) -> None:
    for _ in range(3):
        lines = [line.strip() for line in text.splitlines()]
        tag = ""
        for line in lines:
            if line.startswith(("#EXTINF:", "#EXT-X-STREAM-INF:")):
                tag = line
            elif tag and line and not line.startswith("#"):
                resource = line
                break
        else:
            raise TvBoxError("HLS 清单没有可读取的媒体")
        resolved = urljoin(url, resource)
        if tag.startswith("#EXTINF:"):
            sample = await download(
                resolved, allow_http=allow_http, max_bytes=1024, prefix_only=True,
                headers=headers, proxy=proxy,
            )
            head = sample.body[:64].lstrip(b"\xef\xbb\xbf \t\r\n").lower()
            if not sample.body or head.startswith((b"<!doctype", b"<html", b"#extm3u")) or sample.mime in {
                "text/html", "application/json",
            }:
                raise TvBoxError("HLS 分片未返回媒体")
            return
        variant = await download(
            resolved, allow_http=allow_http, max_bytes=256 * 1024, prefix_only=True,
            headers=headers, proxy=proxy,
        )
        text = variant.body.decode("utf-8-sig", errors="replace")
        if validate_m3u8_playlist_text(text) is not None:
            raise TvBoxError("HLS 子清单无效")
        url = variant.url
    raise TvBoxError("HLS 子清单层数超过限制")
