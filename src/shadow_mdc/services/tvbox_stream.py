"""Same-origin playback for resources issued by the TVBox resolver."""

import asyncio
import re
import secrets
from collections import OrderedDict
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from time import monotonic
from urllib.parse import urljoin

import httpx
from fastapi.responses import Response, StreamingResponse
from starlette.background import BackgroundTask

from .tvbox_http import TvBoxError, http_url, open_remote

HLS = "application/vnd.apple.mpegurl"
_LEASE_SECONDS = 4 * 3600
_RESOURCE_LIMIT = 20000
_MANIFEST_LIMIT = 2 * 1024 * 1024
_URI = re.compile(r'(?<=\bURI=)"([^"]+)"')
_RANGE = re.compile(r"bytes=(?:\d{1,18}-\d{0,18}|-\d{1,18})")
_SAFE_RESPONSE_HEADERS = {
    "Cache-Control": "private, no-store",
    "Content-Security-Policy": "default-src 'none'; sandbox",
    "X-Content-Type-Options": "nosniff",
}


@dataclass(slots=True)
class StreamTarget:
    url: str
    mime: str = ""


@dataclass(slots=True)
class PlaybackSession:
    site_id: str
    prefix: str
    deadline: float
    allow_http: bool
    headers: dict[str, str]
    proxy: httpx.URL | None = None
    resources: dict[str, StreamTarget] = field(default_factory=dict)
    by_url: dict[str, str] = field(default_factory=dict)

    def register(self, url: str, mime: str = "") -> str:
        if url in self.by_url:
            return self.by_url[url]
        if len(self.resources) >= _RESOURCE_LIMIT:
            raise TvBoxError("播放清单资源数量超限")
        key = secrets.token_urlsafe(12)
        self.resources[key] = StreamTarget(url, mime)
        self.by_url[url] = key
        return key

    def rewrite_manifest(self, source_url: str, text: str) -> str:
        def rewrite(value: str) -> str:
            if "{$" in value:
                raise TvBoxError("此播放清单需要变量替换")
            url = str(http_url(urljoin(source_url, value), allow_http=self.allow_http))
            return self.register(url)

        lines = []
        for line in text.splitlines():
            line = line.strip()
            if line.startswith("#"):
                line = _URI.sub(lambda match: f'"{rewrite(match[1])}"', line)
            elif line:
                line = rewrite(line)
            lines.append(line)
        return "\n".join(lines) + "\n"


class BrowserStreams:
    def __init__(self) -> None:
        self.sessions: OrderedDict[str, PlaybackSession] = OrderedDict()
        self.connections = asyncio.Semaphore(24)

    def issue(
        self, site_id: str, url: str, mime: str, headers: dict[str, str], *, allow_http: bool, prefix: str,
        proxy: httpx.URL | None = None,
    ) -> str:
        now = monotonic()
        while self.sessions and (
            len(self.sessions) >= 32 or next(iter(self.sessions.values())).deadline < now
        ):
            self.sessions.popitem(last=False)
        key = secrets.token_urlsafe(24)
        session = PlaybackSession(site_id, prefix, now + _LEASE_SECONDS, allow_http, headers, proxy)
        self.sessions[key] = session
        return f"{prefix}/{key}/{session.register(url, mime)}"

    def lookup(self, key: str, prefix: str) -> PlaybackSession:
        session = self.sessions.get(key)
        if session is None or session.prefix != prefix or session.deadline < monotonic():
            raise LookupError("播放地址已过期")
        return session

    async def response(self, session: PlaybackSession, resource: str, range_value: str | None) -> Response:
        target = session.resources.get(resource)
        if target is None:
            raise LookupError("播放资源不存在")
        if range_value and not _RANGE.fullmatch(range_value):
            return Response(status_code=416, headers=_SAFE_RESPONSE_HEADERS)
        headers = session.headers.copy()
        if range_value and target.mime != HLS:
            headers["Range"] = range_value
        stack = AsyncExitStack()
        try:
            async with asyncio.timeout(12):
                await stack.enter_async_context(self.connections)
                url, upstream = await stack.enter_async_context(
                    open_remote(
                        target.url, allow_http=session.allow_http, headers=headers, proxy=session.proxy,
                    ),
                )
                if upstream.status_code == 416:
                    response_headers = _SAFE_RESPONSE_HEADERS.copy()
                    if content_range := upstream.headers.get("content-range"):
                        response_headers["Content-Range"] = content_range
                    await stack.aclose()
                    return Response(status_code=416, headers=response_headers)
                upstream.raise_for_status()
                chunks = upstream.aiter_raw(chunk_size=65536)
                first = await anext(chunks, b"")
                if target.mime == HLS or first.lstrip(b"\xef\xbb\xbf \t\r\n").startswith(b"#EXTM3U"):
                    if upstream.status_code != 200:
                        raise TvBoxError("播放清单响应不完整")
                    manifest = bytearray(first)
                    async for chunk in chunks:
                        if len(manifest) + len(chunk) > _MANIFEST_LIMIT:
                            raise TvBoxError("播放清单超过大小限制")
                        manifest.extend(chunk)
                    text = manifest.decode("utf-8-sig", errors="replace").lstrip()
                    if not text.startswith("#EXTM3U"):
                        raise TvBoxError("上游未返回 HLS 清单")
                    body = session.rewrite_manifest(str(url), text)
                    await stack.aclose()
                    return Response(body, media_type=HLS, headers=_SAFE_RESPONSE_HEADERS)
                response_headers = (
                    _SAFE_RESPONSE_HEADERS
                    | {"Content-Encoding": "identity"}
                    | {
                        name: upstream.headers[name]
                        for name in ("content-length", "content-range", "accept-ranges")
                        if name in upstream.headers
                    }
                )
        except BaseException:
            await stack.aclose()
            raise

        async def body_chunks() -> AsyncIterator[bytes]:
            try:
                yield first
                async for chunk in chunks:
                    yield chunk
            finally:
                await stack.aclose()

        return StreamingResponse(
            body_chunks(),
            status_code=upstream.status_code,
            media_type=target.mime or "application/octet-stream",
            headers=response_headers,
            background=BackgroundTask(stack.aclose),
        )
