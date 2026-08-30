import hashlib
import ipaddress
import os
import shutil
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
    def __init__(self, root: Path, client: httpx.AsyncClient | None, *, max_bytes: int):
        self._root = root
        self._client = client
        self._max_bytes = max_bytes

    async def acquire(self, work: Work) -> tuple[ArtworkDownloadResult, dict[str, str]]:
        work_root = self._root / work.id
        work_root.mkdir(parents=True, exist_ok=True)
        local_paths = self.adopt_cached(work)
        downloaded = 0
        cached = len(local_paths)
        failed = 0
        errors: list[str] = []
        for item in work.artwork:
            url = item.get("url")
            if not isinstance(url, str) or url in local_paths:
                continue
            try:
                kind = _artwork_kind(str(item.get("kind", "thumb")))
                local_path = item.get("local_path")
                existing = Path(local_path) if isinstance(local_path, str) else None
                path, was_cached = await self._acquire_url(
                    work_root,
                    kind,
                    url,
                    existing=existing,
                )
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

    def adopt_cached(self, work: Work) -> dict[str, str]:
        """Copy legacy cached files into the stable per-work artwork directory."""

        adopted: dict[str, str] = {}
        work_root = self._root / work.id
        for item in work.artwork:
            url = item.get("url")
            local_path = item.get("local_path")
            if not isinstance(url, str) or not isinstance(local_path, str):
                continue
            existing = Path(local_path)
            if not existing.is_file():
                continue
            kind = _artwork_kind(str(item.get("kind", "thumb")))
            cached = next(work_root.glob(f"{kind}.*"), None)
            if cached is None:
                work_root.mkdir(parents=True, exist_ok=True)
                cached = work_root / f"{kind}{_safe_extension(existing.suffix)}"
                if existing.resolve() != cached.resolve():
                    shutil.copy2(existing, cached)
            adopted[url] = str(cached)
        return adopted

    async def _acquire_url(
        self,
        work_root: Path,
        kind: str,
        url: str,
        *,
        existing: Path | None,
    ) -> tuple[Path, bool]:
        _validate_remote_url(url)
        cached = next(work_root.glob(f"{kind}.*"), None)
        if cached is not None and cached.is_file():
            return cached, True
        if existing is not None and existing.is_file():
            extension = _safe_extension(existing.suffix)
            destination = work_root / f"{kind}{extension}"
            if existing.resolve() != destination.resolve():
                shutil.copy2(existing, destination)
            return destination, True

        if self._client is None:
            raise RuntimeError("artwork download client is unavailable")
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            _validate_remote_url(str(response.url))
            content_type = response.headers.get("content-type", "").split(";", 1)[0].casefold()
            downloaded_extension = _CONTENT_EXTENSIONS.get(content_type)
            if downloaded_extension is None:
                raise ValueError(f"unsupported artwork content type: {content_type or 'missing'}")
            declared = response.headers.get("content-length")
            if declared and int(declared) > self._max_bytes:
                raise ValueError("artwork exceeds configured size limit")
            destination = work_root / f"{kind}{downloaded_extension}"
            digest = hashlib.sha256(url.encode()).hexdigest()[:12]
            descriptor, temporary = tempfile.mkstemp(
                prefix=f"{kind}.{digest}.",
                suffix=".tmp",
                dir=work_root,
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


def _artwork_kind(value: str) -> str:
    return "fanart" if value.casefold() in {"fanart", "background", "backdrop"} else "poster"


def _safe_extension(value: str) -> str:
    normalized = value.casefold()
    return normalized if normalized in {".jpg", ".jpeg", ".png", ".webp", ".gif"} else ".jpg"


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
