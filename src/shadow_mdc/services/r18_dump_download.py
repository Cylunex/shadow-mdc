"""Resumable download of the weekly r18.dev dump (``https://r18.dev/dumps/latest``).

Resume logic ported from javinizer/javinizer-go ``internal/r18devdump/download.go``
(MIT): after the ``/dumps/latest`` redirect the dated object URL is requested
directly, and a truncated stream is continued with ``Range: bytes=<offset>-``
guarded by ``If-Range`` (strong ETag, else Last-Modified) so a swapped object is
never spliced. Unlike javinizer we keep a ``.part`` file plus a small JSON sidecar,
so a run killed mid-transfer (or a Cloudflare block on the redirect endpoint)
resumes from disk on the next ``import_r18_dump.py --refresh``.
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..providers.challenge import is_cloudflare_challenge

LATEST_DUMP_URL = "https://r18.dev/dumps/latest"
# r18.dev sits behind Cloudflare, which 403s library default User-Agents.
DOWNLOAD_USER_AGENT = "Mozilla/5.0 (compatible; shadow-mdc/1.0; +https://github.com/Cylunex/shadow-mdc)"
MAX_RESUME_ATTEMPTS = 8
_GZIP_MAGIC = b"\x1f\x8b"
_DUMP_DATE = re.compile(r"_dump_(\d{4}-\d{2}-\d{2})\.")


class R18DumpDownloadError(RuntimeError):
    """Download failed in a way the caller should report (blocked, changed, truncated)."""


@dataclass(frozen=True)
class R18DumpDownloadResult:
    path: Path
    final_url: str
    source_date: str | None
    bytes_downloaded: int
    resumed: bool
    unchanged: bool


@dataclass
class _PartState:
    url: str
    validator: str
    total: int | None

    def save(self, path: Path) -> None:
        path.write_text(
            json.dumps({"url": self.url, "validator": self.validator, "total": self.total}),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, path: Path) -> _PartState | None:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(payload, dict):
            return None
        url, validator, total = payload.get("url"), payload.get("validator"), payload.get("total")
        if not isinstance(url, str) or not isinstance(validator, str) or not validator:
            return None
        return cls(url=url, validator=validator, total=total if isinstance(total, int) else None)


def extract_source_date(url: str) -> str | None:
    matched = _DUMP_DATE.search(url.rsplit("/", 1)[-1])
    return matched.group(1) if matched else None


def if_range_validator(etag: str | None, last_modified: str | None) -> str:
    """Strong ETag, else Last-Modified; weak ETags (``W/``) never satisfy If-Range."""

    if etag and not etag.startswith("W/"):
        return etag
    return last_modified or ""


def parse_content_range(value: str | None) -> tuple[int, int | None]:
    """``bytes <start>-<end>/<total|*>`` → ``(start, total)``."""

    if not value or not value.startswith("bytes "):
        raise R18DumpDownloadError(f"malformed Content-Range {value!r}")
    body = value[len("bytes ") :]
    span, _, total_part = body.rpartition("/")
    start_text, dash, _end = span.partition("-")
    if not dash or not start_text.isdigit():
        raise R18DumpDownloadError(f"malformed Content-Range {value!r}")
    total = None if total_part == "*" else int(total_part) if total_part.isdigit() else None
    if total is None and total_part != "*":
        raise R18DumpDownloadError(f"malformed Content-Range {value!r}")
    return int(start_text), total


def _dump_name(final_url: str) -> str:
    name = final_url.split("?", 1)[0].rstrip("/").rsplit("/", 1)[-1] or "r18dev_dump_latest.sql.gz"
    if not name.endswith(".gz"):
        name = f"{name}.sql.gz"
    return name


def _raise_if_blocked(response: httpx.Response, *, stage: str) -> None:
    content_type = response.headers.get("content-type", "").casefold()
    if "html" not in content_type and response.status_code < 400:
        return
    try:
        body = response.read().decode("utf-8", "replace")[:20000]
    except httpx.HTTPError:
        body = ""
    if is_cloudflare_challenge(body):
        raise R18DumpDownloadError(
            f"{stage}: Cloudflare challenge (HTTP {response.status_code}); retry later or use --proxy"
        )
    if response.status_code >= 400:
        raise R18DumpDownloadError(f"{stage}: HTTP {response.status_code}")
    raise R18DumpDownloadError(f"{stage}: unexpected HTML response instead of a .sql.gz dump")


def download_latest_dump(
    target_dir: Path,
    *,
    client: httpx.Client,
    latest_url: str = LATEST_DUMP_URL,
    progress: Callable[[int, int | None], None] | None = None,
    sleep: Callable[[float], None] = time.sleep,
    max_resume_attempts: int = MAX_RESUME_ATTEMPTS,
) -> R18DumpDownloadResult:
    """Download (or resume) the latest dump into ``target_dir``; skip when already complete."""

    target_dir.mkdir(parents=True, exist_ok=True)
    # identity + iter_raw: byte offsets must match the object on S3 exactly, and raw
    # chunks are written as they arrive so a reset loses nothing already received.
    headers = {"User-Agent": DOWNLOAD_USER_AGENT, "Accept": "*/*", "Accept-Encoding": "identity"}
    with client.stream("GET", latest_url, headers=headers, follow_redirects=True) as response:
        _raise_if_blocked(response, stage="dump endpoint")
        if response.status_code != 200:
            raise R18DumpDownloadError(f"dump endpoint: HTTP {response.status_code}")
        final_url = str(response.url)
        destination = target_dir / _dump_name(final_url)
        total = int(response.headers["content-length"]) if response.headers.get("content-length", "").isdigit() else None
        validator = if_range_validator(response.headers.get("etag"), response.headers.get("last-modified"))
        part = destination.with_name(destination.name + ".part")
        meta = destination.with_name(destination.name + ".part.json")

        if destination.is_file() and (total is None or destination.stat().st_size == total):
            part.unlink(missing_ok=True)
            meta.unlink(missing_ok=True)
            return R18DumpDownloadResult(
                path=destination,
                final_url=final_url,
                source_date=extract_source_date(final_url),
                bytes_downloaded=0,
                resumed=False,
                unchanged=True,
            )

        previous = _PartState.load(meta)
        offset = part.stat().st_size if part.is_file() else 0
        resumable_from_disk = (
            offset > 0
            and previous is not None
            and previous.url == final_url
            and previous.validator == validator
            and validator != ""
            and (total is None or previous.total in {None, total})
        )
        state = _PartState(url=final_url, validator=validator, total=total)
        downloaded = 0
        resumed = False
        clean_eof = False
        if not resumable_from_disk:
            offset = 0
            if validator:
                state.save(meta)
            with part.open("wb") as handle:
                try:
                    for chunk in response.iter_raw():
                        handle.write(chunk)
                        offset += len(chunk)
                        downloaded += len(chunk)
                        if progress is not None:
                            progress(offset, total)
                    clean_eof = True
                except httpx.HTTPError:
                    pass  # truncated stream: resumed below
        else:
            resumed = True

    failures = 0
    while (total is None and not clean_eof) or (total is not None and offset < total):
        if not validator:
            raise R18DumpDownloadError(
                f"dump stream interrupted at {offset} bytes and the server sent no strong "
                "ETag/Last-Modified, so it cannot be resumed safely; re-run --refresh"
            )
        if failures >= max_resume_attempts:
            raise R18DumpDownloadError(f"resume failed after {failures} attempts at {offset} bytes")
        if failures:
            sleep(min(10.0, 0.5 * (2 ** (failures - 1))))
        failures += 1
        range_headers = {**headers, "Range": f"bytes={offset}-", "If-Range": validator}
        try:
            with client.stream("GET", final_url, headers=range_headers, follow_redirects=True) as response:
                if response.status_code == 416:
                    # Requested range starts at EOF: the file is complete.
                    size: int | None = None
                    content_range = response.headers.get("content-range", "")
                    if content_range.startswith("bytes */") and content_range[8:].isdigit():
                        size = int(content_range[8:])
                    if size is not None and size == offset:
                        total = size
                        break
                    raise R18DumpDownloadError(f"resume rejected with HTTP 416 at {offset} bytes")
                if response.status_code == 200:
                    raise R18DumpDownloadError(
                        "server ignored the Range request (HTTP 200); the dump object changed "
                        "mid-download — delete the .part file and re-run --refresh"
                    )
                if response.status_code != 206:
                    _raise_if_blocked(response, stage="dump resume")
                start, remote_total = parse_content_range(response.headers.get("content-range"))
                if start != offset:
                    raise R18DumpDownloadError(f"Content-Range starts at {start}, expected {offset}")
                if total is not None and remote_total is not None and remote_total != total:
                    raise R18DumpDownloadError(
                        f"dump object changed mid-download: size {remote_total}, started with {total}"
                    )
                if remote_total is not None:
                    total = remote_total
                    state.total = total
                    state.save(meta)
                progressed = False
                with part.open("ab") as handle:
                    try:
                        for chunk in response.iter_raw():
                            handle.write(chunk)
                            offset += len(chunk)
                            downloaded += len(chunk)
                            progressed = progressed or bool(chunk)
                            if progress is not None:
                                progress(offset, total)
                        clean_eof = True
                    except httpx.HTTPError:
                        clean_eof = False
                if progressed:
                    failures = 0
        except httpx.HTTPError:
            continue

    with part.open("rb") as handle:
        if handle.read(2) != _GZIP_MAGIC:
            raise R18DumpDownloadError("downloaded dump is not gzip data")
    part.replace(destination)
    meta.unlink(missing_ok=True)
    return R18DumpDownloadResult(
        path=destination,
        final_url=final_url,
        source_date=extract_source_date(final_url),
        bytes_downloaded=downloaded,
        resumed=resumed,
        unchanged=False,
    )
