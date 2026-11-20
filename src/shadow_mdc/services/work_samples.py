"""Enrich works with multi-frame samples: provider galleries first, local ffmpeg fallback."""

from __future__ import annotations

import asyncio
import shutil
import tempfile
from dataclasses import dataclass
from pathlib import Path

import httpx

from ..db.models import MediaAsset, Work
from ..db.repository import Repository
from ..media.artwork import ArtworkDownloadResult, ArtworkStore
from ..media.screenshots import DEFAULT_SAMPLE_RATIOS, capture_sample_frames


@dataclass(frozen=True)
class SampleEnrichResult:
    work_id: str
    web_downloaded: int
    web_cached: int
    local_generated: int
    sample_count: int
    skipped_strm: bool = False
    skipped_no_media: bool = False
    errors: tuple[str, ...] = ()


def count_samples(work: Work) -> int:
    return sum(1 for item in work.artwork if str(item.get("kind", "")).casefold() == "sample")


def sample_urls_for_work(work: Work) -> list[str]:
    """Stable API paths for sample stills (local cache preferred)."""

    urls: list[str] = []
    index = 0
    for item in work.artwork:
        if str(item.get("kind", "")).casefold() != "sample":
            continue
        local = item.get("local_path")
        if isinstance(local, str) and Path(local).is_file():
            urls.append(f"/api/works/{work.id}/samples/{index}")
            index += 1
            continue
        remote = item.get("url")
        if isinstance(remote, str) and remote.startswith(("http://", "https://")):
            urls.append(remote)
            index += 1
    return urls


async def acquire_work_artwork(
    repo: Repository, store: ArtworkStore, work: Work,
) -> tuple[Work, ArtworkDownloadResult]:
    work_id = work.id
    snapshot = Work(id=work_id, artwork=[dict(item) for item in work.artwork])
    repo.commit()
    result, local_paths = await store.acquire(snapshot)
    if local_paths:
        latest = repo.begin_artwork_update(work_id)
        if latest is None:
            repo.rollback()
            raise ValueError("作品已被移除")
        original = {item.get("url"): item.get("local_path") for item in snapshot.artwork if isinstance(item.get("url"), str)}
        filtered = {url: path for url, path in local_paths.items() if any(
            item.get("url") == url and item.get("local_path") == original.get(url) for item in latest.artwork
        )}
        if filtered and "artwork" not in (latest.field_locks or []):
            repo.update_artwork_local_paths(latest, filtered)
        work = latest
        repo.commit()
    return work, result


async def enrich_work_samples(
    repo: Repository,
    work: Work,
    *,
    artwork_root: Path,
    http_client: httpx.AsyncClient | None,
    max_bytes: int,
    target_count: int = 5,
    ratios: tuple[float, ...] = DEFAULT_SAMPLE_RATIOS,
    asset: MediaAsset | None = None,
) -> SampleEnrichResult:
    """
    Prefer provider sample/preview images already on the work (and download them).
    Only extract local ffmpeg frames when local media exists and samples are still short.
    """

    errors: list[str] = []
    store = ArtworkStore(artwork_root, http_client, max_bytes=max_bytes)
    # Download remote artwork (samples first via store kind handling; posters also cached).
    work_id = work.id
    work, result = await acquire_work_artwork(repo, store, work)

    sample_total = count_samples(work)
    local_generated = 0
    skipped_strm = False
    skipped_no_media = False

    if sample_total >= target_count:
        return SampleEnrichResult(
            work_id=work.id,
            web_downloaded=result.downloaded,
            web_cached=result.cached,
            local_generated=0,
            sample_count=sample_total,
            errors=result.errors,
        )

    # Pick a local media asset for ffmpeg fallback.
    chosen = asset
    if chosen is None:
        for candidate in repo.list_assets_for_work(work.id):
            suffix = Path(candidate.path).suffix.casefold()
            if suffix == ".strm":
                skipped_strm = True
                continue
            if Path(candidate.path).is_file():
                chosen = candidate
                break
    if chosen is None:
        skipped_no_media = True
        return SampleEnrichResult(
            work_id=work.id,
            web_downloaded=result.downloaded,
            web_cached=result.cached,
            local_generated=0,
            sample_count=sample_total,
            skipped_strm=skipped_strm,
            skipped_no_media=skipped_no_media,
            errors=tuple([*result.errors, *errors])[:20],
        )
    if Path(chosen.path).suffix.casefold() == ".strm":
        return SampleEnrichResult(
            work_id=work.id,
            web_downloaded=result.downloaded,
            web_cached=result.cached,
            local_generated=0,
            sample_count=sample_total,
            skipped_strm=True,
            errors=result.errors,
        )

    needed = max(0, target_count - sample_total)
    if needed <= 0:
        return SampleEnrichResult(
            work_id=work.id,
            web_downloaded=result.downloaded,
            web_cached=result.cached,
            local_generated=0,
            sample_count=sample_total,
            errors=result.errors,
        )

    samples_root = artwork_root / work_id / "samples"
    samples_root.mkdir(parents=True, exist_ok=True)
    samples_dir = Path(tempfile.mkdtemp(prefix="frames-", dir=samples_root))
    asset_id, input_path, duration = chosen.id, chosen.path, chosen.duration_seconds
    repo.commit()
    try:
        frames = await asyncio.to_thread(
            capture_sample_frames,
            input_path,
            samples_dir,
            duration_seconds=duration,
            ratios=ratios,
            limit=needed,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        shutil.rmtree(samples_dir, ignore_errors=True)
        errors.append(f"{input_path}: {exc}")
        return SampleEnrichResult(
            work_id=work.id,
            web_downloaded=result.downloaded,
            web_cached=result.cached,
            local_generated=0,
            sample_count=sample_total,
            errors=tuple([*result.errors, *errors])[:20],
        )

    payload = [
        {
            "kind": "sample",
            "local_path": str(frame.path),
            "source": "local-sample",
            "asset_id": asset_id,
            "index": frame.index,
            "ratio": frame.ratio,
            "timestamp_seconds": frame.timestamp_seconds,
        }
        for frame in frames
    ]
    latest = repo.begin_artwork_update(work_id)
    current_asset = repo.get_asset(asset_id)
    if not latest or not current_asset or current_asset.work_id != work_id or current_asset.path != input_path or "artwork" in (latest.field_locks or []):
        repo.rollback()
        shutil.rmtree(samples_dir, ignore_errors=True)
        return SampleEnrichResult(work_id, result.downloaded, result.cached, 0, sample_total,
                                  errors=(*result.errors, "抽帧期间文件归属或作品锁已变化"))
    payload = payload[:max(0, target_count - count_samples(latest))]
    if payload:
        repo.set_sample_frames(latest, samples=payload)
    repo.commit()
    if not payload:
        shutil.rmtree(samples_dir, ignore_errors=True)
    local_generated = len(payload)
    sample_total = count_samples(latest)
    return SampleEnrichResult(
        work_id=work.id,
        web_downloaded=result.downloaded,
        web_cached=result.cached,
        local_generated=local_generated,
        sample_count=sample_total,
        skipped_strm=skipped_strm,
        skipped_no_media=skipped_no_media,
        errors=tuple([*result.errors, *errors])[:20],
    )
