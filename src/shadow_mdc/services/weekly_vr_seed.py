"""Weekly VR Top~10 catalog fill: scan VR popularity sources and seed missing works.

Not limited to JAV. Prefer box seed → ``scripts/sync_catalog_to_nas.sh`` / ``--sync-nas``
(same pattern as daily chart/hot). Magnets save-only; no 115.

Sources (soft-skip when blocked; never abort the whole run):

1. **FANZA/DMM VR rankings** via ``FanzaProvider.fetch_ranking`` (``rankings_weekly_vr``
   preferred; daily optional). GraphQL ``floor=VR`` is tried then degraded to AV
   ranking filtered to VR titles/cids when the enum rejects VR.
2. **Non-JAV VR** public pages when reachable:
   - SexLikeReal popular scenes HTML (title/URL, no login required for listing)
   - DeoVR homepage trending/top-picks cards
   - Sukebei ``q=VR`` seeders list (often JAV VR codes; still useful as fallback)

Does **not** replace the curated yearly ``jav-vr-yearly-top`` seed.

Routine CLI (box)::

    PYTHONPATH=src .venv/bin/python scripts/seed_weekly_vr.py --limit 10 --dry-run
    PYTHONPATH=src .venv/bin/python scripts/seed_weekly_vr.py --limit 10 --sync-nas
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import Awaitable, Callable, Sequence
from datetime import UTC, date, datetime
from pathlib import Path
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser

from ..db.repository import Repository
from ..domain import Artwork, ProviderRecord
from ..enums import ContentFamily, MediaCategory
from ..identity import extract_code
from ..media.artwork import ArtworkStore
from ..providers.base import ProviderError
from ..providers.fanza import FanzaProvider, FanzaRankingItem, is_fanza_vr_item
from .daily_chart_seed import merge_tags
from .daily_hot_seed import extract_codes_from_text, parse_sukebei_list_html
from .discover import DiscoverService

logger = logging.getLogger(__name__)

SOURCE_WEIGHTS: dict[str, int] = {
    "fanza_vr_weekly": 5,
    "fanza_vr_daily": 3,
    "slr_popular": 3,
    "deovr_trending": 2,
    "sukebei_vr": 2,
}

DEFAULT_TAGS: tuple[str, ...] = ("weekly-vr", "vr")

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

_SLR_POPULAR_URL = "https://www.sexlikereal.com/api/scenes?sort=mostPopular&limit=24"
_DEOVR_HOME_URL = "https://deovr.com/"
_SUKEBEI_VR_URL = "https://sukebei.nyaa.si/?f=0&c=2_2&q=VR&s=seeders&o=desc"

FetchText = Callable[[str], Awaitable[str]]


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    ok: bool
    items: int = 0
    detail: str = ""


class VrCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    provider: str
    external_id: str
    source_url: str
    code: str | None = None
    title: str
    thumb_url: str | None = None
    score: float = 0.0
    sources: tuple[str, ...] = ()
    state: str = "not_in_library"
    actresses: tuple[str, ...] = ()


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
    source_provider: str = ""


class SkippedCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str
    code: str | None
    title: str
    reason: str
    score: float = 0.0
    state: str | None = None


class SeedFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    key: str | None
    code: str | None
    title: str | None
    error: str


class WeeklyVrSeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_date: str
    dry_run: bool
    limit: int
    sources: tuple[SourceStatus, ...]
    considered: tuple[VrCandidate, ...]
    seeded: tuple[SeededWorkSummary, ...]
    skipped: tuple[SkippedCandidate, ...]
    failures: tuple[SeedFailure, ...]
    run_log_path: str | None = None


class _RawHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    external_id: str
    source_url: str
    code: str | None = None
    title: str
    thumb_url: str | None = None
    source: str
    weight: int = Field(ge=1)
    rank_hint: int = Field(default=1, ge=1)
    actresses: tuple[str, ...] = ()


def list_rank_points(rank: int) -> float:
    if rank < 1:
        raise ValueError("rank must be >= 1")
    return max(1.0, 51.0 - float(rank))


def weekly_vr_tags(run_day: date, rank: int) -> tuple[str, ...]:
    return (
        *DEFAULT_TAGS,
        f"weekly-vr-{run_day.isoformat()}",
        f"weekly-vr-rank-{rank}",
    )


def candidate_key(*, code: str | None, provider: str, external_id: str) -> str:
    if code:
        return code.strip().upper()
    return f"{provider}:{external_id}"


def score_hits(hits: Sequence[_RawHit]) -> list[VrCandidate]:
    buckets: dict[str, dict[str, object]] = {}
    for hit in hits:
        key = candidate_key(code=hit.code, provider=hit.provider, external_id=hit.external_id)
        bucket = buckets.get(key)
        points = float(hit.weight) * list_rank_points(hit.rank_hint)
        if bucket is None:
            buckets[key] = {
                "key": key,
                "provider": hit.provider,
                "external_id": hit.external_id,
                "source_url": hit.source_url,
                "code": hit.code,
                "title": hit.title,
                "thumb_url": hit.thumb_url,
                "score": points,
                "sources": {hit.source},
                "actresses": list(hit.actresses),
            }
            continue
        bucket["score"] = float(bucket["score"]) + points
        sources = bucket["sources"]
        assert isinstance(sources, set)
        sources.add(hit.source)
        if hit.code and not bucket["code"]:
            bucket["code"] = hit.code
        if hit.title and (not bucket["title"] or len(hit.title) > len(str(bucket["title"]))):
            bucket["title"] = hit.title
        if hit.thumb_url and not bucket["thumb_url"]:
            bucket["thumb_url"] = hit.thumb_url
        # Prefer FANZA provider identity when available for seeding.
        if hit.provider == "fanza":
            bucket["provider"] = hit.provider
            bucket["external_id"] = hit.external_id
            bucket["source_url"] = hit.source_url
        if hit.actresses and not bucket["actresses"]:
            bucket["actresses"] = list(hit.actresses)

    candidates: list[VrCandidate] = []
    for bucket in buckets.values():
        sources = bucket["sources"]
        assert isinstance(sources, set)
        actresses = bucket["actresses"]
        assert isinstance(actresses, list)
        candidates.append(
            VrCandidate(
                key=str(bucket["key"]),
                provider=str(bucket["provider"]),
                external_id=str(bucket["external_id"]),
                source_url=str(bucket["source_url"]),
                code=bucket["code"] if isinstance(bucket["code"], str) else None,
                title=str(bucket["title"]),
                thumb_url=bucket["thumb_url"] if isinstance(bucket["thumb_url"], str) else None,
                score=float(bucket["score"]),
                sources=tuple(sorted(sources)),
                actresses=tuple(str(a) for a in actresses if a),
            )
        )
    candidates.sort(key=lambda c: (-c.score, c.code or c.key))
    return candidates


def select_seed_targets(
    candidates: Sequence[VrCandidate],
    *,
    limit: int = 10,
    existing_keys: set[str] | None = None,
) -> tuple[list[VrCandidate], list[SkippedCandidate]]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    known = {key.strip().upper() for key in (existing_keys or set()) if key.strip()}
    selected: list[VrCandidate] = []
    skipped: list[SkippedCandidate] = []
    for candidate in candidates:
        if candidate.state in {"in_library", "catalog_only"}:
            skipped.append(
                SkippedCandidate(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    reason=f"already_{candidate.state}",
                    score=candidate.score,
                    state=candidate.state,
                )
            )
            continue
        key = candidate.key.strip().upper()
        if key in known:
            skipped.append(
                SkippedCandidate(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    reason="existing_work",
                    score=candidate.score,
                    state=candidate.state,
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
                    score=candidate.score,
                    state=candidate.state,
                )
            )
            continue
        selected.append(candidate)
        known.add(key)
    return selected, skipped


def persist_run_log(
    data_dir: Path, result: WeeklyVrSeedResult, *, filename: str | None = None
) -> Path:
    root = data_dir / "weekly-vr-runs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (filename or f"{result.run_date}.json")
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


def parse_slr_popular_html(html: str, *, source: str = "slr_popular") -> list[_RawHit]:
    """Parse SexLikeReal popular listing HTML (api/scenes SSR page) for title/URL."""

    if _is_antibot_html(html):
        raise RuntimeError("SLR returned antibot/captcha page")
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    root = HTMLParser(html)
    hits: list[_RawHit] = []
    seen: set[str] = set()
    rank = 0
    for anchor in root.css("a[href*='/scenes/']"):
        href = (anchor.attributes.get("href") or "").strip()
        if not href or "#" in href:
            continue
        title = anchor.text(strip=True) or ""
        if not title or len(title) < 2:
            continue
        if href in seen:
            continue
        seen.add(href)
        rank += 1
        absolute = href if href.startswith("http") else urljoin("https://www.sexlikereal.com/", href)
        slug = absolute.rstrip("/").split("/")[-1]
        codes = extract_codes_from_text(title)
        code = codes[0] if codes else None
        hits.append(
            _RawHit(
                provider="slr",
                external_id=slug or f"slr-{rank}",
                source_url=absolute,
                code=code,
                title=title,
                source=source,
                weight=weight,
                rank_hint=rank,
            )
        )
    return hits


def parse_deovr_trending_html(html: str, *, source: str = "deovr_trending") -> list[_RawHit]:
    """Parse DeoVR homepage article cards (trending / top picks) for title/URL."""

    if _is_antibot_html(html):
        raise RuntimeError("DeoVR returned antibot/captcha page")
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    root = HTMLParser(html)
    hits: list[_RawHit] = []
    seen: set[str] = set()
    rank = 0
    for article in root.css("article"):
        link = article.css_first("a[href]")
        if link is None:
            continue
        href = (link.attributes.get("href") or "").strip()
        if not href or href.startswith("/categories") or href.startswith("/channel"):
            continue
        # Video cards use short relative paths like /3g8hos
        if href.startswith("http") and "deovr.com/" not in href.casefold():
            continue
        heading = article.css_first("h2, h3, h4, .title")
        title = (
            heading.text(strip=True)
            if heading is not None
            else (link.text(strip=True) or "")
        )
        if not title:
            continue
        path = href if href.startswith("/") else f"/{href.rstrip('/').split('/')[-1]}"
        if not re.fullmatch(r"/[A-Za-z0-9_-]{3,20}", path):
            continue
        if path in seen:
            continue
        seen.add(path)
        rank += 1
        absolute = urljoin("https://deovr.com/", path)
        external_id = path.strip("/")
        hits.append(
            _RawHit(
                provider="deovr",
                external_id=external_id,
                source_url=absolute,
                code=None,
                title=title,
                source=source,
                weight=weight,
                rank_hint=rank,
            )
        )
    return hits


def fanza_items_to_hits(
    items: Sequence[FanzaRankingItem],
    *,
    source: str,
) -> list[_RawHit]:
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    hits: list[_RawHit] = []
    for item in items:
        if not is_fanza_vr_item(item):
            # fetch_ranking already filters for *_vr lists; keep guard for callers.
            continue
        hits.append(
            _RawHit(
                provider="fanza",
                external_id=item.content_id,
                source_url=item.source_url,
                code=item.code,
                title=item.title,
                thumb_url=item.thumb_url,
                source=source,
                weight=weight,
                rank_hint=max(1, item.rank),
                actresses=item.actresses,
            )
        )
    return hits


def sukebei_vr_hits(html: str, *, source: str = "sukebei_vr") -> list[_RawHit]:
    """Map sukebei VR search rows into VR hits (prefer codes that look VR-ish)."""

    mentions = parse_sukebei_list_html(html, source=source)
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    hits: list[_RawHit] = []
    for rank, mention in enumerate(mentions, start=1):
        code = mention.code
        hits.append(
            _RawHit(
                provider="javdb",
                external_id=code,
                source_url=mention.source_url or f"https://sukebei.nyaa.si/?q={code}",
                code=code,
                title=mention.title_hint or code,
                source=source,
                weight=weight,
                rank_hint=rank,
            )
        )
    return hits


def _is_antibot_html(html: str) -> bool:
    lowered = html.casefold()
    markers = (
        "verifying your browser",
        "antibot",
        "just a moment",
        "cf-browser-verification",
        "attention required",
        "enable javascript and cookies",
    )
    return any(marker in lowered for marker in markers)


async def default_fetch_text(client: httpx.AsyncClient, url: str) -> str:
    response = await client.get(
        url,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "text/html,application/json;q=0.9,*/*;q=0.8",
            "Accept-Language": "ja,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
        },
    )
    response.raise_for_status()
    return response.text


async def collect_vr_hits(
    *,
    fanza: FanzaProvider | None,
    fetch: FetchText,
    prefer_weekly: bool = True,
) -> tuple[list[_RawHit], tuple[SourceStatus, ...]]:
    """Gather VR popularity hits; each source failure is recorded and skipped."""

    hits: list[_RawHit] = []
    statuses: list[SourceStatus] = []

    ranking_jobs: list[tuple[str, str]] = []
    if prefer_weekly:
        ranking_jobs.append(("fanza_vr_weekly", "rankings_weekly_vr"))
        ranking_jobs.append(("fanza_vr_daily", "rankings_daily_vr"))
    else:
        ranking_jobs.append(("fanza_vr_daily", "rankings_daily_vr"))
        ranking_jobs.append(("fanza_vr_weekly", "rankings_weekly_vr"))

    if fanza is None:
        for source, _list_name in ranking_jobs:
            statuses.append(
                SourceStatus(source=source, ok=False, items=0, detail="fanza provider unavailable")
            )
    else:
        for source, list_name in ranking_jobs:
            try:
                items = await fanza.fetch_ranking(list_name, limit=40, floor="VR")
                batch = fanza_items_to_hits(items, source=source)
                hits.extend(batch)
                statuses.append(
                    SourceStatus(source=source, ok=True, items=len(batch), detail="ok")
                )
            except Exception as exc:
                statuses.append(
                    SourceStatus(
                        source=source,
                        ok=False,
                        items=0,
                        detail=f"{type(exc).__name__}: {exc}",
                    )
                )

    html_jobs: tuple[tuple[str, str, Callable[[str], list[_RawHit]]], ...] = (
        ("slr_popular", _SLR_POPULAR_URL, parse_slr_popular_html),
        ("deovr_trending", _DEOVR_HOME_URL, parse_deovr_trending_html),
        ("sukebei_vr", _SUKEBEI_VR_URL, sukebei_vr_hits),
    )
    for source, url, parser in html_jobs:
        try:
            html = await fetch(url)
            batch = parser(html)
            hits.extend(batch)
            statuses.append(SourceStatus(source=source, ok=True, items=len(batch), detail="ok"))
        except Exception as exc:
            statuses.append(
                SourceStatus(
                    source=source,
                    ok=False,
                    items=0,
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )

    return hits, tuple(statuses)


async def seed_weekly_vr(
    *,
    discover: DiscoverService,
    repo: Repository,
    data_dir: Path,
    http_client: httpx.AsyncClient | None,
    fanza: FanzaProvider | None = None,
    limit: int = 10,
    dry_run: bool = False,
    run_day: date | None = None,
    download_posters: bool = True,
    artwork_max_bytes: int = 25 * 1024 * 1024,
    persist_log: bool = True,
    fetch_text: FetchText | None = None,
    prefer_weekly: bool = True,
) -> WeeklyVrSeedResult:
    """Collect VR popularity, pick top N not already in library, seed + tag + posters."""

    day = run_day or date.today()
    if fetch_text is None:
        if http_client is None:
            raise ValueError("http_client or fetch_text is required")

        async def _fetch(url: str) -> str:
            return await default_fetch_text(http_client, url)

        fetch = _fetch
    else:
        fetch = fetch_text

    raw_hits, source_statuses = await collect_vr_hits(
        fanza=fanza, fetch=fetch, prefer_weekly=prefer_weekly
    )
    considered = score_hits(raw_hits)

    projected: list[VrCandidate] = []
    existing_keys: set[str] = set()
    for candidate in considered:
        work = None
        if candidate.code:
            work = repo.find_work_by_code(candidate.code)
        if work is None:
            work = repo.find_work_by_provider_identity(candidate.provider, candidate.external_id)
        if work is None:
            projected.append(candidate)
            continue
        has_media = bool(repo.list_assets_for_work(work.id))
        state = "in_library" if has_media else "catalog_only"
        existing_keys.add(candidate.key.strip().upper())
        projected.append(candidate.model_copy(update={"state": state}))

    targets, skipped = select_seed_targets(
        projected, limit=limit, existing_keys=existing_keys
    )

    seeded: list[SeededWorkSummary] = []
    failures: list[SeedFailure] = []
    for status in source_statuses:
        if not status.ok:
            failures.append(
                SeedFailure(
                    key=status.source,
                    code=None,
                    title=status.source,
                    error=f"source_skipped: {status.detail}",
                )
            )

    for rank, candidate in enumerate(targets, start=1):
        tags = weekly_vr_tags(day, rank)
        if dry_run:
            seeded.append(
                SeededWorkSummary(
                    work_id="dry-run",
                    code=candidate.code,
                    title=candidate.title,
                    actors=candidate.actresses,
                    rank=rank,
                    score=candidate.score,
                    created=True,
                    tags=tags,
                    source_provider=candidate.provider,
                )
            )
            continue
        try:
            work_id, created, work_title, work_code, actors = await _seed_candidate(
                discover=discover,
                repo=repo,
                candidate=candidate,
            )
            work = repo.get_work(work_id)
            if work is None:
                raise LookupError(f"seeded work missing: {work_id}")
            updated_tags = merge_tags(work.tags or [], tags)
            repo.update_work_fields(work, tags=updated_tags, lock_edited=False)
            artwork_downloaded = 0
            artwork_failed = 0
            if download_posters and http_client is not None:
                # Prefer provider artwork; else candidate thumb.
                if not work.artwork and candidate.thumb_url:
                    try:
                        record = ProviderRecord(
                            provider=candidate.provider,
                            external_id=candidate.external_id,
                            source_url=candidate.source_url,
                            code=candidate.code,
                            title=work.title,
                            family=ContentFamily.JAV
                            if candidate.code
                            else ContentFamily.UNKNOWN,
                            category=MediaCategory.JAPAN
                            if candidate.code
                            else MediaCategory.OTHER,
                            artwork=(Artwork(url=candidate.thumb_url, kind="poster"),),
                            tags=tuple(updated_tags),
                        )
                        work = repo.upsert_provider_record(record, overwrite=False)
                    except Exception:
                        logger.debug("thumb attach failed for %s", candidate.key, exc_info=True)
                if work.artwork:
                    art_result, local_paths = await ArtworkStore(
                        data_dir / "artwork",
                        http_client,
                        max_bytes=artwork_max_bytes,
                    ).acquire(work)
                    if local_paths:
                        repo.update_artwork_local_paths(work, local_paths)
                    artwork_downloaded = art_result.downloaded
                    artwork_failed = art_result.failed
            seeded.append(
                SeededWorkSummary(
                    work_id=work.id,
                    code=work.primary_code or work_code or candidate.code,
                    title=work.title or work_title,
                    actors=actors or tuple(a for a in (work.actors or []) if a),
                    rank=rank,
                    score=candidate.score,
                    created=created,
                    tags=tuple(updated_tags),
                    artwork_downloaded=artwork_downloaded,
                    artwork_failed=artwork_failed,
                    source_provider=candidate.provider,
                )
            )
        except Exception as exc:
            failures.append(
                SeedFailure(
                    key=candidate.key,
                    code=candidate.code,
                    title=candidate.title,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    result = WeeklyVrSeedResult(
        run_date=day.isoformat(),
        dry_run=dry_run,
        limit=limit,
        sources=source_statuses,
        considered=tuple(projected),
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


async def _seed_candidate(
    *,
    discover: DiscoverService,
    repo: Repository,
    candidate: VrCandidate,
) -> tuple[str, bool, str, str | None, tuple[str, ...]]:
    """Seed via DiscoverService when possible; else upsert a lightweight ProviderRecord."""

    if candidate.provider in {"fanza", "javdb"} or candidate.code:
        try:
            seed_result = await discover.seed(
                repo,
                provider=candidate.provider if candidate.provider in {"fanza", "javdb"} else "javdb",
                external_id=candidate.external_id if candidate.provider in {"fanza", "javdb"} else None,
                source_url=candidate.source_url if candidate.provider == "javdb" else None,
                code=candidate.code,
            )
            work = repo.get_work(seed_result.work_id)
            actors = tuple(a for a in ((work.actors if work else None) or []) if a)
            return (
                seed_result.work_id,
                seed_result.created,
                seed_result.title,
                seed_result.primary_code,
                actors or candidate.actresses,
            )
        except (LookupError, ValueError, ProviderError) as exc:
            # Fall through to direct upsert for non-resolvable / non-JAV rows.
            logger.debug("discover.seed fallback for %s: %s", candidate.key, exc)

    artwork: tuple[Artwork, ...] = ()
    if candidate.thumb_url:
        artwork = (Artwork(url=candidate.thumb_url, kind="poster"),)
    family = ContentFamily.JAV if candidate.code else ContentFamily.UNKNOWN
    category = MediaCategory.JAPAN if candidate.code else MediaCategory.OTHER
    # Ensure code parses when present.
    code = candidate.code
    if code:
        parsed, _family = extract_code(code)
        code = parsed or code
    record = ProviderRecord(
        provider=candidate.provider,
        external_id=candidate.external_id,
        source_url=candidate.source_url,
        code=code,
        title=candidate.title,
        original_title=candidate.title,
        family=family,
        category=category,
        actors=candidate.actresses,
        tags=("vr",),
        artwork=artwork,
    )
    existing = None
    if code:
        existing = repo.find_work_by_code(code)
    if existing is None:
        existing = repo.find_work_by_provider_identity(candidate.provider, candidate.external_id)
    created = existing is None
    work = repo.upsert_provider_record(record, overwrite=False)
    return work.id, created, work.title, work.primary_code, tuple(a for a in (work.actors or []) if a)


def utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()
