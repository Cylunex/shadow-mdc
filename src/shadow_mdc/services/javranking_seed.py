"""Seed missing catalog works from the JavRanking static search index."""

from __future__ import annotations

import json
import logging
from collections.abc import Sequence
from datetime import UTC, date, datetime
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict

from ..db.repository import Repository
from ..media.artwork import ArtworkStore
from ..normalize_code import normalize_code, to_comparison_key
from .daily_chart_seed import merge_tags
from .discover import DiscoverService
from .javranking_client import (
    DEFAULT_BASE_URL,
    DEFAULT_LOCALE,
    JavRankingIndexCache,
    SearchVideo,
)

logger = logging.getLogger(__name__)

DEFAULT_TAG = "javranking"


class JavRankingCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    video_id: int
    title: str
    rank: int | None = None
    score: float = 0.0
    ranking_slugs: tuple[str, ...] = ()
    state: str = "not_in_library"


class SeededWorkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    code: str | None
    title: str
    actors: tuple[str, ...] = ()
    rank: int | None = None
    score: float = 0.0
    created: bool
    tags: tuple[str, ...] = ()
    artwork_downloaded: int = 0
    artwork_failed: int = 0
    video_id: int | None = None


class SkippedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    title: str
    reason: str
    rank: int | None = None
    score: float = 0.0
    state: str | None = None


class SeedFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None
    title: str | None
    error: str


class JavRankingSeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_date: str
    dry_run: bool
    limit: int
    revision: str | None = None
    considered: tuple[JavRankingCandidate, ...] = ()
    seeded: tuple[SeededWorkSummary, ...] = ()
    skipped: tuple[SkippedCandidate, ...] = ()
    failures: tuple[SeedFailure, ...] = ()
    run_log_path: str | None = None


def ranking_tags(video: SearchVideo, *, extra_slugs: Sequence[str] | None = None) -> tuple[str, ...]:
    tags = [DEFAULT_TAG]
    slugs = [item.slug for item in video.ranking_appearances if item.slug]
    if extra_slugs:
        slugs = [*slugs, *extra_slugs]
    for slug in slugs:
        cleaned = slug.strip()
        if cleaned and cleaned not in tags:
            tags.append(cleaned)
    return tuple(tags)


def candidate_sort_key(video: SearchVideo) -> tuple[int, float, str]:
    """Prefer overall rank (1 first); unranked fall after, then higher score."""

    rank = video.rank if video.rank is not None else 10**9
    return (rank, -float(video.score or 0.0), to_comparison_key(video.code or ""))


def videos_for_seed(
    videos: Sequence[SearchVideo],
    *,
    min_rank: int | None = None,
    ranking_slug: str | None = None,
) -> list[SearchVideo]:
    selected: list[SearchVideo] = []
    seen_keys: set[str] = set()
    for video in sorted(videos, key=candidate_sort_key):
        raw_code = (video.code or "").strip()
        if not raw_code:
            continue
        code = normalize_code(raw_code) or raw_code
        key = to_comparison_key(code)
        if not key or key in seen_keys:
            continue
        if min_rank is not None and (video.rank is None or video.rank > min_rank):
            continue
        if ranking_slug:
            slug = ranking_slug.strip().casefold()
            if not any(item.slug.casefold() == slug for item in video.ranking_appearances):
                continue
        seen_keys.add(key)
        selected.append(
            video.model_copy(update={"code": code}) if video.code != code else video
        )
    return selected


def select_missing_targets(
    videos: Sequence[SearchVideo],
    *,
    limit: int,
    existing_keys: set[str],
) -> tuple[list[SearchVideo], list[SkippedCandidate]]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    known = {key for key in existing_keys if key}
    selected: list[SearchVideo] = []
    skipped: list[SkippedCandidate] = []
    for video in videos:
        code = normalize_code(video.code or "") or (video.code or "")
        key = to_comparison_key(code)
        title = video.title
        if key in known:
            skipped.append(
                SkippedCandidate(
                    code=code,
                    title=title,
                    reason="existing_work_by_code",
                    rank=video.rank,
                    score=float(video.score or 0.0),
                    state="catalog_or_library",
                )
            )
            continue
        if len(selected) >= limit:
            skipped.append(
                SkippedCandidate(
                    code=code,
                    title=title,
                    reason="beyond_limit",
                    rank=video.rank,
                    score=float(video.score or 0.0),
                )
            )
            continue
        selected.append(video)
        known.add(key)
    return selected, skipped


def persist_run_log(
    data_dir: Path,
    result: JavRankingSeedResult,
    *,
    filename: str | None = None,
) -> Path:
    root = data_dir / "javranking-runs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (filename or f"{result.run_date}.json")
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


async def seed_javranking(
    *,
    discover: DiscoverService,
    repo: Repository,
    data_dir: Path,
    http_client: httpx.AsyncClient | None,
    limit: int = 50,
    dry_run: bool = False,
    run_day: date | None = None,
    download_posters: bool = True,
    artwork_max_bytes: int = 25 * 1024 * 1024,
    persist_log: bool = True,
    force_refresh: bool = False,
    min_rank: int | None = None,
    ranking_slug: str | None = None,
    seed_provider: str = "javdb",
    base_url: str = DEFAULT_BASE_URL,
    locale: str = DEFAULT_LOCALE,
    index: object | None = None,
) -> JavRankingSeedResult:
    """Fill missing works from JavRanking index, ordered by overall rank."""

    day = run_day or date.today()
    cache = JavRankingIndexCache(
        data_dir / "javranking",
        client=http_client,
        base_url=base_url,
        locale=locale,
    )

    revision: str | None = None
    if index is None:
        loaded = await cache.load_index(force_refresh=force_refresh)
        meta = cache.read_meta()
        revision = meta.revision if meta is not None else None
    else:
        from .javranking_client import SearchIndex, parse_search_index

        loaded = index if isinstance(index, SearchIndex) else parse_search_index(index)  # type: ignore[arg-type]
        # Refresh honors map from provided index for offline/tests.
        raw = loaded.model_dump_json(by_alias=True)
        cache.write_cache(index=loaded, raw_text=raw, revision="inline")
        revision = "inline"

    filtered = videos_for_seed(loaded.videos, min_rank=min_rank, ranking_slug=ranking_slug)

    existing_keys: set[str] = set()
    considered: list[JavRankingCandidate] = []
    for video in filtered:
        code = normalize_code(video.code or "") or (video.code or "")
        key = to_comparison_key(code)
        work = repo.find_work_by_code(code) if code else None
        state = "not_in_library"
        if work is not None:
            existing_keys.add(key)
            has_media = bool(repo.list_assets_for_work(work.id))
            state = "in_library" if has_media else "catalog_only"
        considered.append(
            JavRankingCandidate(
                code=code,
                video_id=video.video_id,
                title=video.title,
                rank=video.rank,
                score=float(video.score or 0.0),
                ranking_slugs=tuple(item.slug for item in video.ranking_appearances),
                state=state,
            )
        )

    # Prefer overall rank among missing-only fill.
    missing_videos = [
        video
        for video in filtered
        if to_comparison_key(normalize_code(video.code or "") or (video.code or "")) not in existing_keys
    ]
    targets, skipped = select_missing_targets(
        missing_videos, limit=limit, existing_keys=existing_keys
    )
    # Also record already-present as skipped when they appear in filtered list.
    for candidate in considered:
        if candidate.state != "not_in_library":
            if any(s.code == candidate.code and s.reason == "existing_work_by_code" for s in skipped):
                continue
            skipped.append(
                SkippedCandidate(
                    code=candidate.code,
                    title=candidate.title,
                    reason=f"already_{candidate.state}",
                    rank=candidate.rank,
                    score=candidate.score,
                    state=candidate.state,
                )
            )

    seeded: list[SeededWorkSummary] = []
    failures: list[SeedFailure] = []

    for video in targets:
        code = normalize_code(video.code or "") or (video.code or "")
        tags = ranking_tags(video)
        if dry_run:
            seeded.append(
                SeededWorkSummary(
                    work_id="dry-run",
                    code=code,
                    title=video.title,
                    actors=tuple(link.name for link in video.actor_links if link.name),
                    rank=video.rank,
                    score=float(video.score or 0.0),
                    created=True,
                    tags=tags,
                    video_id=video.video_id,
                )
            )
            continue
        try:
            seed_result = await discover.seed(
                repo,
                provider=seed_provider,
                code=code,
            )
            work = repo.get_work(seed_result.work_id)
            if work is None:
                raise LookupError(f"seeded work missing: {seed_result.work_id}")
            updated_tags = merge_tags(work.tags or [], tags)
            repo.update_work_fields(work, tags=updated_tags, lock_edited=False)
            artwork_downloaded = 0
            artwork_failed = 0
            if download_posters and http_client is not None and work.artwork:
                art_result, local_paths = await ArtworkStore(
                    data_dir / "artwork",
                    http_client,
                    max_bytes=artwork_max_bytes,
                ).acquire(work)
                if local_paths:
                    repo.update_artwork_local_paths(work, local_paths)
                artwork_downloaded = art_result.downloaded
                artwork_failed = art_result.failed
            actors = tuple(a for a in (work.actors or []) if a)
            seeded.append(
                SeededWorkSummary(
                    work_id=work.id,
                    code=work.primary_code or code,
                    title=work.title,
                    actors=actors,
                    rank=video.rank,
                    score=float(video.score or 0.0),
                    created=seed_result.created,
                    tags=tuple(updated_tags),
                    artwork_downloaded=artwork_downloaded,
                    artwork_failed=artwork_failed,
                    video_id=video.video_id,
                )
            )
        except Exception as exc:
            failures.append(
                SeedFailure(
                    code=code,
                    title=video.title,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    result = JavRankingSeedResult(
        run_date=day.isoformat(),
        dry_run=dry_run,
        limit=limit,
        revision=revision,
        considered=tuple(considered),
        seeded=tuple(seeded),
        skipped=tuple(skipped),
        failures=tuple(failures),
    )
    if persist_log:
        log_name = f"{day.isoformat()}-dry-run.json" if dry_run else f"{day.isoformat()}.json"
        path = persist_run_log(data_dir, result, filename=log_name)
        result = result.model_copy(update={"run_log_path": str(path)})
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_log_path"] = str(path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
