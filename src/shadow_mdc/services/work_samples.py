"""Enrich works with multi-frame samples: provider galleries first, local ffmpeg fallback."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import httpx

from ..db.models import MediaAsset, Work
from ..db.repository import Repository
from ..media.artwork import ArtworkStore
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
    result, local_paths = await store.acquire(work)
    if local_paths:
        repo.update_artwork_local_paths(work, local_paths)

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

    samples_dir = artwork_root / work.id / "samples"
    try:
        frames = capture_sample_frames(
            chosen.path,
            samples_dir,
            duration_seconds=chosen.duration_seconds,
            ratios=ratios,
            limit=needed,
        )
    except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
        errors.append(f"{chosen.path}: {exc}")
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
            "asset_id": chosen.id,
            "index": frame.index,
            "ratio": frame.ratio,
            "timestamp_seconds": frame.timestamp_seconds,
        }
        for frame in frames
    ]
    repo.set_sample_frames(work, samples=payload, replace_local=True)
    local_generated = len(payload)
    sample_total = count_samples(work)
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
