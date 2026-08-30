import hashlib
import ipaddress
import os
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from ..db.models import Work

_CONTENT_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class ArtworkDownloadResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    downloaded: int
    cached: int
    failed: int
    errors: tuple[str, ...]


class ArtworkStore:
    def __init__(self, root: Path, client: httpx.AsyncClient, *, max_bytes: int):
        self._root = root
        self._client = client
        self._max_bytes = max_bytes

    async def acquire(self, work: Work) -> tuple[ArtworkDownloadResult, dict[str, str]]:
        self._root.mkdir(parents=True, exist_ok=True)
        local_paths: dict[str, str] = {}
        downloaded = 0
        cached = 0
        failed = 0
        errors: list[str] = []
        for item in work.artwork:
            url = item.get("url")
            if not isinstance(url, str) or url in local_paths:
                continue
            try:
                path, was_cached = await self._acquire_url(url)
                local_paths[url] = str(path)
                cached += int(was_cached)
                downloaded += int(not was_cached)
            except (ValueError, OSError, httpx.HTTPError) as exc:
                failed += 1
                if len(errors) < 20:
                    errors.append(f"{url}: {type(exc).__name__}: {exc}")
        return (
            ArtworkDownloadResult(
                work_id=work.id,
                downloaded=downloaded,
                cached=cached,
                failed=failed,
                errors=tuple(errors),
            ),
            local_paths,
        )

    async def _acquire_url(self, url: str) -> tuple[Path, bool]:
        _validate_remote_url(url)
        digest = hashlib.sha256(url.encode()).hexdigest()
        existing = next(self._root.glob(f"{digest}.*"), None)
        if existing is not None and existing.is_file():
            return existing, True

        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            _validate_remote_url(str(response.url))
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            extension = _CONTENT_EXTENSIONS.get(content_type)
            if extension is None:
                raise ValueError(f"unsupported artwork content type: {content_type or 'missing'}")
            declared = response.headers.get("content-length")
            if declared and int(declared) > self._max_bytes:
                raise ValueError("artwork exceeds configured size limit")
            destination = self._root / f"{digest}{extension}"
            descriptor, temporary = tempfile.mkstemp(
                prefix=f"{digest}.",
                suffix=".tmp",
                dir=self._root,
            )
            size = 0
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    async for chunk in response.aiter_bytes():
                        size += len(chunk)
                        if size > self._max_bytes:
                            raise ValueError("artwork exceeds configured size limit")
                        stream.write(chunk)
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, destination)
            except Exception:
                Path(temporary).unlink(missing_ok=True)
                raise
        return destination, False


def _validate_remote_url(url: str) -> None:
    parsed = urlsplit(url)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("artwork URL must use HTTP or HTTPS")
    hostname = parsed.hostname.casefold().rstrip(".")
    if hostname == "localhost" or hostname.endswith(".local"):
        raise ValueError("local artwork hosts are not allowed")
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return
    if not address.is_global:
        raise ValueError("private artwork addresses are not allowed")
