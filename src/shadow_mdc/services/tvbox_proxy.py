"""HTTP CONNECT for an already validated public media destination."""

import ipaddress
from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from functools import lru_cache
from ssl import SSLContext

import httpcore
import httpx


class ConnectBackend(httpcore.AnyIOBackend):
    def __init__(self, proxy: httpx.URL) -> None:
        self.proxy = proxy

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Iterable[httpcore.SOCKET_OPTION] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        address = ipaddress.ip_address(host)
        if not address.is_global or address.is_multicast:
            raise httpcore.ProxyError("CONNECT requires a public destination")
        authority = f"[{host}]:{port}" if address.version == 6 else f"{host}:{port}"
        stream = await super().connect_tcp(
            self.proxy.host, self.proxy.port or 80, timeout, local_address, socket_options,
        )
        try:
            await stream.write(f"CONNECT {authority} HTTP/1.1\r\nHost: {authority}\r\n\r\n".encode(), timeout)
            header = bytearray()
            while len(header) < 16384:
                chunk = await stream.read(4096, timeout)
                if not chunk:
                    break
                header.extend(chunk)
                head, end, tail = header.partition(b"\r\n\r\n")
                if end:
                    status_line = head.split(b"\r\n", 1)[0].split()
                    if len(head) <= 16384 and not tail and len(status_line) >= 2 and status_line[1] == b"200":
                        return stream
                    break
            raise httpcore.ProxyError("CONNECT was refused")
        except BaseException:
            await stream.aclose()
            raise


class ProxyBody(httpx.AsyncByteStream):
    def __init__(self, stream: AsyncIterator[bytes]) -> None:
        self.stream = stream

    async def __aiter__(self) -> AsyncIterator[bytes]:
        async for chunk in self.stream:
            yield chunk


@lru_cache(maxsize=1)
def _tls_context() -> SSLContext:
    return httpcore.default_ssl_context()


@asynccontextmanager
async def open_proxy(proxy: httpx.URL, request: httpx.Request) -> AsyncIterator[httpx.Response]:
    # Keep the connection origin pinned to the public IP. The caller also passes
    # the original host as SNI, so certificate verification uses that host.
    url = request.url
    origin = httpcore.Origin(url.raw_scheme, url.raw_host, url.port or (443 if url.scheme == "https" else 80))
    connection = httpcore.AsyncHTTPConnection(
        origin=origin, ssl_context=_tls_context(), network_backend=ConnectBackend(proxy),
    )
    try:
        result = await connection.handle_async_request(httpcore.Request(
            b"GET",
            httpcore.URL(scheme=url.raw_scheme, host=url.raw_host, port=url.port, target=url.raw_path),
            headers=request.headers.raw,
            extensions=request.extensions,
        ))
        try:
            yield httpx.Response(
                result.status, headers=result.headers,
                stream=ProxyBody(result.aiter_stream()), request=request,
            )
        finally:
            await result.aclose()
    except (
        httpcore.ProxyError, httpcore.NetworkError, httpcore.TimeoutException, httpcore.ProtocolError,
    ) as exc:
        raise httpx.RequestError("TVBox proxy connection failed", request=request) from exc
    finally:
        await connection.aclose()
