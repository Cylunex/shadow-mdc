import hashlib
import ipaddress
import os
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from PIL import Image

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
                if kind != "sample":
                    ensure_list_thumbnail(work_root, path)
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
            if kind == "sample":
                samples_root = work_root / "samples"
                samples_root.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(url.encode()).hexdigest()[:12]
                cached = samples_root / f"sample_{digest}{_safe_extension(existing.suffix)}"
                if existing.resolve() != cached.resolve():
                    shutil.copy2(existing, cached)
                adopted[url] = str(cached)
                continue
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
        if kind == "sample":
            return await self._acquire_sample(work_root, url, existing=existing)

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

    async def _acquire_sample(
        self,
        work_root: Path,
        url: str,
        *,
        existing: Path | None,
    ) -> tuple[Path, bool]:
        samples_root = work_root / "samples"
        samples_root.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(url.encode()).hexdigest()[:12]
        cached = next(samples_root.glob(f"sample_{digest}.*"), None)
        if cached is not None and cached.is_file():
            return cached, True
        if existing is not None and existing.is_file():
            extension = _safe_extension(existing.suffix)
            destination = samples_root / f"sample_{digest}{extension}"
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
            destination = samples_root / f"sample_{digest}{downloaded_extension}"
            descriptor, temporary = tempfile.mkstemp(
                prefix=f"sample.{digest}.",
                suffix=".tmp",
                dir=samples_root,
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
    lowered = value.casefold()
    if lowered in {"fanart", "background", "backdrop"}:
        return "fanart"
    if lowered == "sample":
        return "sample"
    return "poster"


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


LIST_THUMB_MAX_WIDTH = 480


def ensure_list_thumbnail(work_root: Path, source: Path, *, max_width: int = LIST_THUMB_MAX_WIDTH) -> Path | None:
    """Create data/artwork/<work>/thumb.jpg for faster works-grid rendering."""

    if not source.is_file():
        return None
    existing = next(work_root.glob("thumb.*"), None)
    if existing is not None and existing.is_file():
        return existing
    try:
        with Image.open(source) as image:
            image = image.convert("RGB")
            width, height = image.size
            if width > max_width:
                ratio = max_width / float(width)
                image = image.resize((max_width, max(1, int(height * ratio))), Image.Resampling.LANCZOS)
            destination = work_root / "thumb.jpg"
            temporary = work_root / "thumb.jpg.tmp"
            image.save(temporary, format="JPEG", quality=85, optimize=True)
            temporary.replace(destination)
            return destination
    except OSError:
        return None


_FANART_KINDS = frozenset({"fanart", "background", "backdrop"})
_SAMPLE_KINDS = frozenset({"sample"})


def artwork_dir_for_work(work: Work, data_dir: Path | None = None) -> Path | None:
    """Resolve ``data/artwork/<work_id>`` without mistaking ``samples/`` for the root."""

    if data_dir is not None:
        return data_dir / "artwork" / work.id
    for item in work.artwork:
        local = item.get("local_path")
        if not isinstance(local, str) or not local:
            continue
        path = Path(local)
        if not path.is_file():
            continue
        candidate = path.parent.parent if path.parent.name == "samples" else path.parent
        if candidate.name == work.id:
            return candidate
    return None


def _stem_exists(root: Path, stem: str) -> bool:
    return any(path.is_file() for path in root.glob(f"{stem}.*"))


def _remote_cover_urls(work: Work, *, fanart_only: bool) -> list[str]:
    urls: list[str] = []
    for item in work.artwork:
        kind = str(item.get("kind", "thumb")).casefold()
        if kind in _SAMPLE_KINDS:
            continue
        is_fanart = kind in _FANART_KINDS
        if fanart_only and not is_fanart:
            continue
        if not fanart_only and is_fanart:
            continue
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            urls.append(url)
    return urls


def resolve_work_display_image(
    work: Work,
    kind: str = "poster",
    *,
    data_dir: Path | None = None,
) -> str | None:
    """Browser-usable cover URL for list/detail cards.

    Prefer on-disk ``thumb`` / ``poster`` / ``fanart`` under ``data/artwork/<id>``.
    Cached **sample** frames must not count as a poster — that caused lists to emit
    ``/api/works/.../artwork/poster`` (404) when only samples/fanart/thumb existed.
    """

    root = artwork_dir_for_work(work, data_dir)
    if kind == "fanart":
        if root is not None and _stem_exists(root, "fanart"):
            return f"/api/works/{work.id}/artwork/fanart"
        return next(iter(_remote_cover_urls(work, fanart_only=True)), None)

    if root is not None:
        if _stem_exists(root, "thumb"):
            return f"/api/works/{work.id}/artwork/thumb"
        if _stem_exists(root, "poster"):
            return f"/api/works/{work.id}/artwork/poster"
        if _stem_exists(root, "fanart"):
            return f"/api/works/{work.id}/artwork/fanart"

    preferred = [
        item
        for item in work.artwork
        if item.get("preferred") is True
        and str(item.get("kind", "thumb")).casefold() not in _FANART_KINDS | _SAMPLE_KINDS
        and isinstance(item.get("local_path"), str)
        and Path(str(item["local_path"])).is_file()
    ]
    if preferred:
        parent = Path(str(preferred[0]["local_path"])).parent
        if parent.name == "samples":
            parent = parent.parent
        if _stem_exists(parent, "poster"):
            return f"/api/works/{work.id}/artwork/poster"
        if _stem_exists(parent, "thumb"):
            return f"/api/works/{work.id}/artwork/thumb"

    for item in work.artwork:
        kind_name = str(item.get("kind", "thumb")).casefold()
        if kind_name in _FANART_KINDS | _SAMPLE_KINDS:
            continue
        local = item.get("local_path")
        if not isinstance(local, str):
            continue
        path = Path(local)
        if not path.is_file():
            continue
        parent = path.parent.parent if path.parent.name == "samples" else path.parent
        if _stem_exists(parent, "thumb"):
            return f"/api/works/{work.id}/artwork/thumb"
        if _stem_exists(parent, "poster"):
            return f"/api/works/{work.id}/artwork/poster"

    return next(
        iter(_remote_cover_urls(work, fanart_only=False) or _remote_cover_urls(work, fanart_only=True)),
        None,
    )
