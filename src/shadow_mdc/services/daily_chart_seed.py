"""Daily multi-list chart aggregation: score browse lists and seed top works.

Product intent: each day scan available discover rankings, synthesize a combined
ranking by multi-list consensus, and seed the top N works (default 10) plus their
actors into the catalog. Prefer seeding on a host that can fetch rankings (box/FANZA GraphQL when
JavDB is blocked), then incremental sync via ``scripts/sync_catalog_to_nas.sh``.

NAS routine (after deploy of this commit)::

    ssh nas 'cd /data/project/shadow-mdc/current && \\
      SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\
      SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\
      PYTHONPATH=src .venv/bin/python scripts/seed_daily_chart.py --limit 10'
"""

from __future__ import annotations

import json
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Mapping, Sequence

import httpx
from pydantic import BaseModel, ConfigDict, Field

from ..db.repository import Repository
from ..media.artwork import ArtworkStore
from .discover import DiscoverItem, DiscoverList, DiscoverService

# Higher weight = fresher / more authoritative list signal.
LIST_WEIGHTS: dict[str, int] = {
    "rankings_daily": 4,
    "rankings_weekly": 3,
    "rankings_monthly": 2,
    "latest": 1,
    "fanza_rankings_daily": 4,
    "fanza_rankings_weekly": 3,
    "fanza_rankings_monthly": 2,
    "fanza_latest": 1,
}

DEFAULT_LISTS: tuple[DiscoverList, ...] = (
    "rankings_daily",
    "rankings_weekly",
    "rankings_monthly",
    "latest",
)

DEFAULT_BROWSE_PROVIDERS: tuple[str, ...] = ("javdb", "fanza")

# Rank 1 → 50 points, rank 50 → 1; deeper ranks keep a fractional floor.
_RANK_POINT_CEILING = 51.0


class ChartAppearance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    list_name: str
    rank: int = Field(ge=1)
    weight: int = Field(ge=1)
    points: float = Field(ge=0)


class ChartCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    provider: str
    external_id: str
    source_url: str
    code: str | None = None
    title: str
    state: str = "not_in_library"
    thumb_url: str | None = None
    appearances: tuple[ChartAppearance, ...] = ()
    score: float = 0.0
    list_count: int = 0


class SeededWorkSummary(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    code: str | None
    title: str
    actors: tuple[str, ...] = ()
    rank: int
    score: float
    created: bool
    tags: tuple[str, ...] = ()
    artwork_downloaded: int = 0
    artwork_failed: int = 0


class SkippedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    code: str | None
    title: str
    reason: str
    state: str | None = None
    score: float = 0.0


class SeedFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    code: str | None
    title: str
    error: str


class DailyChartSeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_date: str
    dry_run: bool
    limit: int
    lists_scanned: tuple[str, ...]
    considered: tuple[ChartCandidate, ...]
    seeded: tuple[SeededWorkSummary, ...]
    skipped: tuple[SkippedCandidate, ...]
    failures: tuple[SeedFailure, ...]
    run_log_path: str | None = None


def list_rank_points(rank: int) -> float:
    """Map 1-based list position to points (better rank → higher)."""

    if rank < 1:
        raise ValueError("rank must be >= 1")
    return max(1.0, _RANK_POINT_CEILING - float(rank))


def appearance_score(
    list_name: str,
    rank: int,
    *,
    weights: Mapping[str, int] | None = None,
) -> float:
    table = weights or LIST_WEIGHTS
    weight = int(table.get(list_name, 1))
    return float(weight) * list_rank_points(rank)


def candidate_key(item: DiscoverItem) -> str:
    if item.code:
        return item.code.strip().upper()
    return f"{item.provider}:{item.external_id}"


def _state_rank(state: str) -> int:
    order = {"not_in_library": 0, "catalog_only": 1, "in_library": 2}
    return order.get(state, 0)


def score_browse_pages(
    pages: Mapping[str, Sequence[DiscoverItem]],
    *,
    weights: Mapping[str, int] | None = None,
) -> list[ChartCandidate]:
    """Merge items across lists and compute consensus scores.

    Score rises when a title appears on more lists, on higher-weight lists
    (daily > weekly > monthly > latest), and at better (lower) ranks.
    """

    table = dict(LIST_WEIGHTS if weights is None else weights)
    buckets: dict[str, dict[str, object]] = {}
    provider_index: dict[str, str] = {}

    for list_name, items in pages.items():
        weight = int(table.get(list_name, 1))
        for index, item in enumerate(items, start=1):
            code = item.code.strip().upper() if item.code else None
            provider_key = f"{item.provider}:{item.external_id}"
            key = code or provider_key

            if code and key not in buckets and provider_key in provider_index:
                old_key = provider_index[provider_key]
                if old_key in buckets and old_key != key:
                    buckets[key] = buckets.pop(old_key)
                    buckets[key]["key"] = key
                    buckets[key]["code"] = item.code

            bucket = buckets.get(key)
            if bucket is None and provider_key in provider_index:
                mapped = provider_index[provider_key]
                bucket = buckets.get(mapped)
                if bucket is not None:
                    key = mapped

            if bucket is None:
                bucket = {
                    "key": key,
                    "provider": item.provider,
                    "external_id": item.external_id,
                    "source_url": item.source_url,
                    "code": item.code,
                    "title": item.title,
                    "state": item.state,
                    "thumb_url": item.thumb_url,
                    "appearances": [],
                    "score": 0.0,
                }
                buckets[key] = bucket
            else:
                if bucket["code"] is None and item.code:
                    bucket["code"] = item.code
                    if code and key != code and code not in buckets:
                        buckets[code] = buckets.pop(key)
                        key = code
                        bucket = buckets[key]
                        bucket["key"] = code
                if item.title and (
                    not bucket["title"] or len(item.title) > len(str(bucket["title"]))
                ):
                    bucket["title"] = item.title
                if item.thumb_url and not bucket["thumb_url"]:
                    bucket["thumb_url"] = item.thumb_url
                if _state_rank(item.state) > _state_rank(str(bucket["state"])):
                    bucket["state"] = item.state
                bucket["provider"] = item.provider
                bucket["external_id"] = item.external_id
                bucket["source_url"] = item.source_url

            provider_index[provider_key] = key
            points = appearance_score(list_name, index, weights=table)
            appearances = bucket["appearances"]
            assert isinstance(appearances, list)
            appearances.append(
                ChartAppearance(
                    list_name=list_name, rank=index, weight=weight, points=points
                )
            )
            bucket["score"] = float(bucket["score"]) + points

    candidates: list[ChartCandidate] = []
    for bucket in buckets.values():
        raw_appearances = bucket["appearances"]
        assert isinstance(raw_appearances, list)
        appearances = tuple(sorted(raw_appearances, key=lambda a: (-a.weight, a.rank)))
        candidates.append(
            ChartCandidate(
                key=str(bucket["key"]),
                provider=str(bucket["provider"]),
                external_id=str(bucket["external_id"]),
                source_url=str(bucket["source_url"]),
                code=bucket["code"] if isinstance(bucket["code"], str) else None,
                title=str(bucket["title"]),
                state=str(bucket["state"]),
                thumb_url=bucket["thumb_url"] if isinstance(bucket["thumb_url"], str) else None,
                appearances=appearances,
                score=float(bucket["score"]),
                list_count=len(appearances),
            )
        )
    candidates.sort(key=lambda c: (-c.score, -c.list_count, c.code or c.key))
    return candidates


def select_seed_targets(
    candidates: Sequence[ChartCandidate],
    *,
    limit: int = 10,
    existing_codes: set[str] | None = None,
) -> tuple[list[ChartCandidate], list[SkippedCandidate]]:
    """Pick top ``limit`` not-in-library candidates; report skips."""

    if limit < 1:
        raise ValueError("limit must be >= 1")
    known = {code.strip().upper() for code in (existing_codes or set()) if code.strip()}
    selected: list[ChartCandidate] = []
    skipped: list[SkippedCandidate] = []
    for candidate in candidates:
        if candidate.state in {"in_library", "catalog_only"}:
            skipped.append(
                SkippedCandidate(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    reason=f"already_{candidate.state}",
                    state=candidate.state,
                    score=candidate.score,
                )
            )
            continue
        code_key = (candidate.code or "").strip().upper()
        if code_key and code_key in known:
            skipped.append(
                SkippedCandidate(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    reason="existing_work_by_code",
                    state=candidate.state,
                    score=candidate.score,
                )
            )
            continue
        if len(selected) >= limit:
            skipped.append(
                SkippedCandidate(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    reason="beyond_limit",
                    state=candidate.state,
                    score=candidate.score,
                )
            )
            continue
        selected.append(candidate)
        if code_key:
            known.add(code_key)
    return selected, skipped


def chart_tags(run_day: date, rank: int) -> tuple[str, str]:
    return (f"daily-chart-{run_day.isoformat()}", f"daily-chart-rank-{rank}")


def merge_tags(existing: Sequence[str], extra: Sequence[str]) -> list[str]:
    seen: set[str] = set()
    merged: list[str] = []
    for tag in [*existing, *extra]:
        cleaned = tag.strip()
        if not cleaned or cleaned in seen:
            continue
        seen.add(cleaned)
        merged.append(cleaned)
    return merged


def persist_run_log(data_dir: Path, result: DailyChartSeedResult, *, filename: str | None = None) -> Path:
    root = data_dir / "daily-chart-runs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (filename or f"{result.run_date}.json")
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def browse_page_key(provider: str, list_name: DiscoverList | str) -> str:
    """Map provider+logical list to a scoring bucket (FANZA gets fanza_ prefix)."""

    if provider == "fanza":
        return f"fanza_{list_name}"
    return str(list_name)


async def collect_browse_pages(
    discover: DiscoverService,
    repo: Repository,
    *,
    provider: str | Sequence[str] = DEFAULT_BROWSE_PROVIDERS,
    lists: Sequence[DiscoverList] = DEFAULT_LISTS,
    page: int = 1,
) -> tuple[dict[str, tuple[DiscoverItem, ...]], tuple[str, ...]]:
    """Browse configured lists across providers; one failure must not abort others.

    Tries JavDB and FANZA by default. FANZA rankings use the public DMM GraphQL API
    (works when JavDB is Cloudflare-blocked). Empty pages are skipped.
    """

    providers = (provider,) if isinstance(provider, str) else tuple(provider)
    pages: dict[str, tuple[DiscoverItem, ...]] = {}
    failures: list[str] = []
    for provider_id in providers:
        for list_name in lists:
            key = browse_page_key(provider_id, list_name)
            try:
                result = await discover.browse(
                    repo, provider=provider_id, list_name=list_name, page=page
                )
                if result.items:
                    pages[key] = result.items
                else:
                    failures.append(f"{provider_id}/{list_name}: empty")
            except ValueError as exc:
                failures.append(f"{provider_id}/{list_name}: unsupported ({exc})")
            except Exception as exc:  # noqa: BLE001 - surface per-list, continue others
                failures.append(f"{provider_id}/{list_name}: {type(exc).__name__}: {exc}")
    return pages, tuple(failures)


async def seed_daily_chart(
    *,
    discover: DiscoverService,
    repo: Repository,
    data_dir: Path,
    http_client: httpx.AsyncClient | None,
    limit: int = 10,
    dry_run: bool = False,
    run_day: date | None = None,
    lists: Sequence[DiscoverList] = DEFAULT_LISTS,
    provider: str | Sequence[str] = DEFAULT_BROWSE_PROVIDERS,
    download_posters: bool = True,
    artwork_max_bytes: int = 25 * 1024 * 1024,
    persist_log: bool = True,
) -> DailyChartSeedResult:
    """Browse lists, score consensus ranking, seed top works + artwork + tags."""

    day = run_day or date.today()
    pages, browse_failures = await collect_browse_pages(
        discover, repo, provider=provider, lists=lists
    )
    considered = score_browse_pages(pages)

    existing_codes: set[str] = set()
    for candidate in considered:
        if candidate.code and repo.find_work_by_code(candidate.code) is not None:
            existing_codes.add(candidate.code.strip().upper())

    targets, skipped = select_seed_targets(
        considered, limit=limit, existing_codes=existing_codes
    )

    seeded: list[SeededWorkSummary] = []
    failures: list[SeedFailure] = [
        SeedFailure(key="browse", code=None, title=msg, error=msg) for msg in browse_failures
    ]

    for rank, candidate in enumerate(targets, start=1):
        tags = chart_tags(day, rank)
        if dry_run:
            seeded.append(
                SeededWorkSummary(
                    work_id="dry-run",
                    code=candidate.code,
                    title=candidate.title,
                    actors=(),
                    rank=rank,
                    score=candidate.score,
                    created=True,
                    tags=tags,
                )
            )
            continue
        try:
            seed_result = await discover.seed(
                repo,
                provider=candidate.provider,
                external_id=candidate.external_id,
                source_url=candidate.source_url,
                code=candidate.code,
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
                    code=work.primary_code or candidate.code,
                    title=work.title,
                    actors=actors,
                    rank=rank,
                    score=candidate.score,
                    created=seed_result.created,
                    tags=tuple(updated_tags),
                    artwork_downloaded=artwork_downloaded,
                    artwork_failed=artwork_failed,
                )
            )
        except Exception as exc:  # noqa: BLE001 - continue remaining candidates
            failures.append(
                SeedFailure(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    result = DailyChartSeedResult(
        run_date=day.isoformat(),
        dry_run=dry_run,
        limit=limit,
        lists_scanned=tuple(str(name) for name in pages.keys()),
        considered=tuple(considered),
        seeded=tuple(seeded),
        skipped=tuple(skipped),
        failures=tuple(failures),
    )
    if persist_log:
        log_name = f"{day.isoformat()}-dry-run.json" if dry_run else f"{day.isoformat()}.json"
        path = persist_run_log(data_dir, result, filename=log_name)
        result = result.model_copy(update={"run_log_path": str(path)})
        # Keep JSON run_date canonical even for dry-run filenames.
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["run_log_path"] = str(path)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
