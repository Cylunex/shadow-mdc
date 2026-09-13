"""Daily social/forum buzz aggregation: score hot mentions and seed top works.

Complementary to ``daily_chart_seed`` (official rankings). Primary signal is
cross-source buzz: X/Twitter mirrors, Reddit/community threads, torrent forums,
plus lightly-weighted secondary hot pages. Prefer box seed → NAS sync.
"""

from __future__ import annotations

import json
import logging
import math
import re
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Mapping, Sequence
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser

from ..db.repository import Repository
from ..identity import extract_code
from ..media.artwork import ArtworkStore
from .daily_chart_seed import merge_tags
from .discover import DiscoverService

logger = logging.getLogger(__name__)

# Buzz-oriented weights. Secondary chart-ish lists stay below social/forum hits.
SOURCE_WEIGHTS: dict[str, int] = {
    "x_twitter": 5,
    "reddit": 4,
    "sukebei_seeders": 3,
    "sukebei_recent": 2,
    "freejavbt_day": 2,
    "freejavbt_week": 1,
    "javdb_latest": 1,
    "javlibrary_mostwanted": 1,
}

DEFAULT_TAG = "hot-buzz"

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Loose finder; each hit is re-validated / normalized via extract_code.
_CODE_FINDER = re.compile(
    r"(?i)(?:FC2(?:[-_. ]?PPV)?[-_. ]?\d{5,9})"
    r"|(?:HEYZO[-_. ]?\d{3,5})"
    r"|(?:(?<![A-Z0-9])(?:\d{2,5})?[A-Z]{2,10}[-_. ]?\d{2,6}(?![A-Z0-9]))"
)

_X_MIRROR_URLS: tuple[str, ...] = (
    "https://xcancel.com/search?f=tweets&q={query}",
    "https://nitter.poast.org/search?f=tweets&q={query}",
    "https://nitter.privacydev.net/search?f=tweets&q={query}",
)

_X_QUERIES: tuple[str, ...] = (
    "AV 新作",
    "配信開始",
    "SSIS OR SONE OR MIDV OR IPZZ",
)

_REDDIT_URLS: tuple[str, ...] = (
    "https://www.reddit.com/r/jav/hot.json?limit=50",
    "https://www.reddit.com/r/JapanesePorn/hot.json?limit=25",
    "https://www.reddit.com/r/JAVUncensored/hot.json?limit=25",
)

FetchText = Callable[[str], Awaitable[str]]


class CodeMention(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    source: str
    weight: int = Field(ge=1)
    engagement: float = Field(default=0.0, ge=0.0)
    title_hint: str | None = None
    source_url: str | None = None


class SourceStatus(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    source: str
    ok: bool
    mentions: int = 0
    detail: str = ""


class HotCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str
    score: float = 0.0
    mention_count: int = 0
    sources: tuple[str, ...] = ()
    title_hint: str | None = None
    max_engagement: float = 0.0
    state: str = "not_in_library"


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

    code: str
    title: str | None
    reason: str
    score: float = 0.0
    state: str | None = None


class SeedFailure(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None
    title: str | None
    error: str


class DailyHotSeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    run_date: str
    dry_run: bool
    limit: int
    sources: tuple[SourceStatus, ...]
    considered: tuple[HotCandidate, ...]
    seeded: tuple[SeededWorkSummary, ...]
    skipped: tuple[SkippedCandidate, ...]
    failures: tuple[SeedFailure, ...]
    run_log_path: str | None = None


def extract_codes_from_text(text: str) -> list[str]:
    """Return unique normalized codes found in free text (order preserved)."""

    found: list[str] = []
    seen: set[str] = set()
    for match in _CODE_FINDER.finditer(text or ""):
        code, _family = extract_code(match.group(0))
        if not code:
            continue
        key = code.upper()
        if key in seen:
            continue
        seen.add(key)
        found.append(code)
    return found


def mention_points(weight: int, engagement: float = 0.0) -> float:
    """Weight × (1 + log1p(engagement)) so seeders/upvotes boost without dominating."""

    return float(weight) * (1.0 + math.log1p(max(0.0, engagement)))


def score_mentions(mentions: Sequence[CodeMention]) -> list[HotCandidate]:
    buckets: dict[str, dict[str, object]] = {}
    for mention in mentions:
        code = mention.code.strip().upper()
        if not code:
            continue
        bucket = buckets.get(code)
        if bucket is None:
            bucket = {
                "code": code,
                "score": 0.0,
                "mention_count": 0,
                "sources": set(),
                "title_hint": mention.title_hint,
                "max_engagement": 0.0,
            }
            buckets[code] = bucket
        bucket["score"] = float(bucket["score"]) + mention_points(mention.weight, mention.engagement)
        bucket["mention_count"] = int(bucket["mention_count"]) + 1
        sources = bucket["sources"]
        assert isinstance(sources, set)
        sources.add(mention.source)
        if mention.engagement > float(bucket["max_engagement"]):
            bucket["max_engagement"] = float(mention.engagement)
        if mention.title_hint and (
            not bucket["title_hint"] or len(mention.title_hint) > len(str(bucket["title_hint"]))
        ):
            bucket["title_hint"] = mention.title_hint

    candidates: list[HotCandidate] = []
    for bucket in buckets.values():
        sources = bucket["sources"]
        assert isinstance(sources, set)
        candidates.append(
            HotCandidate(
                code=str(bucket["code"]),
                score=float(bucket["score"]),
                mention_count=int(bucket["mention_count"]),
                sources=tuple(sorted(sources)),
                title_hint=bucket["title_hint"] if isinstance(bucket["title_hint"], str) else None,
                max_engagement=float(bucket["max_engagement"]),
            )
        )
    candidates.sort(key=lambda c: (-c.score, -c.mention_count, -c.max_engagement, c.code))
    return candidates


def select_seed_targets(
    candidates: Sequence[HotCandidate],
    *,
    limit: int = 10,
    existing_codes: set[str] | None = None,
) -> tuple[list[HotCandidate], list[SkippedCandidate]]:
    if limit < 1:
        raise ValueError("limit must be >= 1")
    known = {code.strip().upper() for code in (existing_codes or set()) if code.strip()}
    selected: list[HotCandidate] = []
    skipped: list[SkippedCandidate] = []
    for candidate in candidates:
        if candidate.state in {"in_library", "catalog_only"}:
            skipped.append(
                SkippedCandidate(
                    code=candidate.code,
                    title=candidate.title_hint,
                    reason=f"already_{candidate.state}",
                    score=candidate.score,
                    state=candidate.state,
                )
            )
            continue
        if candidate.code in known:
            skipped.append(
                SkippedCandidate(
                    code=candidate.code,
                    title=candidate.title_hint,
                    reason="existing_work_by_code",
                    score=candidate.score,
                    state=candidate.state,
                )
            )
            continue
        if len(selected) >= limit:
            skipped.append(
                SkippedCandidate(
                    code=candidate.code,
                    title=candidate.title_hint,
                    reason="beyond_limit",
                    score=candidate.score,
                    state=candidate.state,
                )
            )
            continue
        selected.append(candidate)
        known.add(candidate.code)
    return selected, skipped


def hot_tags(run_day: date, rank: int) -> tuple[str, str, str]:
    return (DEFAULT_TAG, f"daily-hot-{run_day.isoformat()}", f"daily-hot-rank-{rank}")


def persist_run_log(data_dir: Path, result: DailyHotSeedResult, *, filename: str | None = None) -> Path:
    root = data_dir / "daily-hot-runs"
    root.mkdir(parents=True, exist_ok=True)
    path = root / (filename or f"{result.run_date}.json")
    path.write_text(result.model_dump_json(indent=2), encoding="utf-8")
    return path


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


def parse_x_mirror_html(html: str, *, source: str = "x_twitter") -> list[CodeMention]:
    if _is_antibot_html(html):
        raise RuntimeError("X mirror returned antibot/captcha page")
    root = HTMLParser(html)
    blobs: list[str] = []
    for sel in (".tweet-content", ".tweet-body", ".timeline-item", "article"):
        for node in root.css(sel):
            text = node.text(separator=" ", strip=True)
            if text:
                blobs.append(text)
    if not blobs:
        # Fallback: whole page text (still filtered by extract_code).
        text = root.text(separator=" ", strip=True)
        if text:
            blobs.append(text)
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    mentions: list[CodeMention] = []
    for blob in blobs:
        for code in extract_codes_from_text(blob):
            mentions.append(
                CodeMention(code=code, source=source, weight=weight, title_hint=blob[:160])
            )
    return mentions


def parse_reddit_hot_json(payload: str | Mapping[str, object], *, source: str = "reddit") -> list[CodeMention]:
    data = json.loads(payload) if isinstance(payload, str) else payload
    if not isinstance(data, Mapping):
        raise ValueError("reddit payload must be an object")
    listing = data.get("data")
    if not isinstance(listing, Mapping):
        # Block / login walls often return HTML wrapped as non-JSON earlier.
        raise ValueError("reddit JSON missing data listing")
    children = listing.get("children") or []
    if not isinstance(children, list):
        raise ValueError("reddit children must be a list")
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    mentions: list[CodeMention] = []
    for child in children:
        if not isinstance(child, Mapping):
            continue
        post = child.get("data")
        if not isinstance(post, Mapping):
            continue
        title = str(post.get("title") or "")
        selftext = str(post.get("selftext") or "")
        blob = f"{title}\n{selftext}".strip()
        score = float(post.get("score") or 0) + float(post.get("num_comments") or 0)
        permalink = str(post.get("permalink") or "")
        url = f"https://www.reddit.com{permalink}" if permalink.startswith("/") else permalink or None
        for code in extract_codes_from_text(blob):
            mentions.append(
                CodeMention(
                    code=code,
                    source=source,
                    weight=weight,
                    engagement=max(0.0, score),
                    title_hint=title[:160] or None,
                    source_url=url,
                )
            )
    return mentions


def parse_sukebei_list_html(
    html: str,
    *,
    source: str,
    base_url: str = "https://sukebei.nyaa.si",
) -> list[CodeMention]:
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    root = HTMLParser(html)
    mentions: list[CodeMention] = []
    for row in root.css("table.torrent-list tbody tr"):
        title_link = row.css_first('a[href*="/view/"]')
        if title_link is None:
            continue
        title = title_link.text(strip=True) or ""
        href = title_link.attributes.get("href") or ""
        source_url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"
        cells = [td.text(strip=True) for td in row.css("td")]
        engagement = 0.0
        # Typical columns: cat | name | size | date | seeders | leechers | downloads
        if len(cells) >= 5:
            for raw in cells[-3:]:
                try:
                    engagement = max(engagement, float(raw.replace(",", "")))
                except ValueError:
                    continue
        for code in extract_codes_from_text(title):
            mentions.append(
                CodeMention(
                    code=code,
                    source=source,
                    weight=weight,
                    engagement=engagement,
                    title_hint=title[:160] or None,
                    source_url=source_url,
                )
            )
    return mentions


def parse_freejavbt_rank_html(html: str, *, source: str) -> list[CodeMention]:
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    root = HTMLParser(html)
    mentions: list[CodeMention] = []
    seen: set[str] = set()
    rank = 0
    for anchor in root.css("a[href]"):
        href = anchor.attributes.get("href") or ""
        match = re.search(r"/([A-Za-z0-9]{2,12}-\d{2,6})(?:/|$|\?)", href)
        raw = match.group(1) if match else (anchor.text(strip=True) or "")
        codes = extract_codes_from_text(raw)
        if not codes:
            continue
        code = codes[0]
        if code in seen:
            continue
        seen.add(code)
        rank += 1
        # Light engagement proxy from list position (rank 1 → higher).
        engagement = max(0.0, 40.0 - float(rank))
        title = anchor.text(strip=True) or code
        mentions.append(
            CodeMention(
                code=code,
                source=source,
                weight=weight,
                engagement=engagement,
                title_hint=title[:160],
                source_url=href if href.startswith("http") else f"https://freejavbt.com{href}",
            )
        )
    return mentions


def parse_javdb_latest_html(html: str, *, source: str = "javdb_latest", base_url: str = "https://javdb.com") -> list[CodeMention]:
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    root = HTMLParser(html)
    mentions: list[CodeMention] = []
    seen: set[str] = set()
    rank = 0
    for node in root.css(".movie-list .item, .grid .item, .item"):
        link = node.css_first("a[href*='/v/']")
        if link is None:
            continue
        code_node = node.css_first("strong") or node.css_first(".uid")
        title_node = node.css_first(".video-title") or node.css_first(".title") or link
        blob = " ".join(
            part
            for part in (
                code_node.text(strip=True) if code_node is not None else "",
                title_node.text(separator=" ", strip=True) if title_node is not None else "",
            )
            if part
        )
        codes = extract_codes_from_text(blob)
        if not codes:
            continue
        code = codes[0]
        if code in seen:
            continue
        seen.add(code)
        rank += 1
        href = link.attributes.get("href") or ""
        source_url = href if href.startswith("http") else f"{base_url.rstrip('/')}{href}"
        mentions.append(
            CodeMention(
                code=code,
                source=source,
                weight=weight,
                engagement=max(0.0, 30.0 - float(rank)),
                title_hint=blob[:160] or None,
                source_url=source_url,
            )
        )
    return mentions


def parse_javlibrary_mostwanted_html(html: str, *, source: str = "javlibrary_mostwanted") -> list[CodeMention]:
    if _is_antibot_html(html):
        raise RuntimeError("JavLibrary returned antibot/captcha page")
    weight = int(SOURCE_WEIGHTS.get(source, 1))
    root = HTMLParser(html)
    mentions: list[CodeMention] = []
    seen: set[str] = set()
    rank = 0
    for item in root.css(".video[id], .video"):
        code_text = ""
        id_node = item.css_first(".id")
        if id_node is not None:
            code_text = id_node.text(strip=True) or ""
        title_node = item.css_first("a")
        title = title_node.text(strip=True) if title_node is not None else code_text
        codes = extract_codes_from_text(f"{code_text} {title}")
        if not codes:
            continue
        code = codes[0]
        if code in seen:
            continue
        seen.add(code)
        rank += 1
        mentions.append(
            CodeMention(
                code=code,
                source=source,
                weight=weight,
                engagement=max(0.0, 30.0 - float(rank)),
                title_hint=title[:160] or None,
            )
        )
    return mentions


async def collect_x_twitter(fetch: FetchText) -> tuple[list[CodeMention], SourceStatus]:
    errors: list[str] = []
    mentions: list[CodeMention] = []
    for query in _X_QUERIES:
        got_any = False
        for template in _X_MIRROR_URLS:
            url = template.format(query=quote(query))
            try:
                html = await fetch(url)
                batch = parse_x_mirror_html(html)
                mentions.extend(batch)
                got_any = True
                break
            except Exception as exc:  # noqa: BLE001 - degrade per mirror
                errors.append(f"{url}: {type(exc).__name__}: {exc}")
        if got_any:
            # One successful query is enough to mark source usable; keep scanning queries.
            continue
    if mentions:
        return mentions, SourceStatus(source="x_twitter", ok=True, mentions=len(mentions), detail="ok")
    detail = "; ".join(errors[:6]) or "no mirrors returned tweets"
    return [], SourceStatus(source="x_twitter", ok=False, mentions=0, detail=detail)


async def collect_reddit(fetch: FetchText) -> tuple[list[CodeMention], SourceStatus]:
    errors: list[str] = []
    mentions: list[CodeMention] = []
    for url in _REDDIT_URLS:
        try:
            payload = await fetch(url)
            if payload.lstrip().startswith("<") or _is_antibot_html(payload):
                raise RuntimeError("reddit returned HTML/block page instead of JSON")
            batch = parse_reddit_hot_json(payload)
            mentions.extend(batch)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {type(exc).__name__}: {exc}")
    if mentions:
        return mentions, SourceStatus(source="reddit", ok=True, mentions=len(mentions), detail="ok")
    detail = "; ".join(errors[:4]) or "reddit unreachable"
    return [], SourceStatus(source="reddit", ok=False, mentions=0, detail=detail)


async def collect_sukebei(fetch: FetchText) -> tuple[list[CodeMention], list[SourceStatus]]:
    statuses: list[SourceStatus] = []
    mentions: list[CodeMention] = []
    targets = (
        ("sukebei_seeders", "https://sukebei.nyaa.si/?f=0&c=2_2&q=&s=seeders&o=desc"),
        ("sukebei_recent", "https://sukebei.nyaa.si/?f=0&c=2_2&q=&s=id&o=desc"),
    )
    for source, url in targets:
        try:
            html = await fetch(url)
            batch = parse_sukebei_list_html(html, source=source)
            mentions.extend(batch)
            statuses.append(SourceStatus(source=source, ok=True, mentions=len(batch), detail="ok"))
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                SourceStatus(source=source, ok=False, mentions=0, detail=f"{type(exc).__name__}: {exc}")
            )
    return mentions, statuses


async def collect_secondary_hot(fetch: FetchText) -> tuple[list[CodeMention], list[SourceStatus]]:
    statuses: list[SourceStatus] = []
    mentions: list[CodeMention] = []
    jobs: tuple[tuple[str, str, Callable[[str], list[CodeMention]]], ...] = (
        (
            "freejavbt_day",
            "https://freejavbt.com/rank/censored/day",
            lambda html: parse_freejavbt_rank_html(html, source="freejavbt_day"),
        ),
        (
            "freejavbt_week",
            "https://freejavbt.com/rank/censored/week",
            lambda html: parse_freejavbt_rank_html(html, source="freejavbt_week"),
        ),
        (
            "javdb_latest",
            "https://javdb.com/",
            lambda html: parse_javdb_latest_html(html, source="javdb_latest"),
        ),
        (
            "javlibrary_mostwanted",
            "https://www.javlibrary.com/en/vl_mostwanted.php",
            lambda html: parse_javlibrary_mostwanted_html(html, source="javlibrary_mostwanted"),
        ),
    )
    for source, url, parser in jobs:
        try:
            html = await fetch(url)
            batch = parser(html)
            mentions.extend(batch)
            statuses.append(SourceStatus(source=source, ok=True, mentions=len(batch), detail="ok"))
        except Exception as exc:  # noqa: BLE001
            statuses.append(
                SourceStatus(source=source, ok=False, mentions=0, detail=f"{type(exc).__name__}: {exc}")
            )
    return mentions, statuses


async def collect_buzz_mentions(
    fetch: FetchText,
) -> tuple[list[CodeMention], tuple[SourceStatus, ...]]:
    """Gather mentions across sources; individual source failures never abort the run."""

    mentions: list[CodeMention] = []
    statuses: list[SourceStatus] = []

    x_mentions, x_status = await collect_x_twitter(fetch)
    mentions.extend(x_mentions)
    statuses.append(x_status)

    reddit_mentions, reddit_status = await collect_reddit(fetch)
    mentions.extend(reddit_mentions)
    statuses.append(reddit_status)

    sukebei_mentions, sukebei_statuses = await collect_sukebei(fetch)
    mentions.extend(sukebei_mentions)
    statuses.extend(sukebei_statuses)

    secondary_mentions, secondary_statuses = await collect_secondary_hot(fetch)
    mentions.extend(secondary_mentions)
    statuses.extend(secondary_statuses)

    return mentions, tuple(statuses)


async def seed_daily_hot(
    *,
    discover: DiscoverService,
    repo: Repository,
    data_dir: Path,
    http_client: httpx.AsyncClient | None,
    limit: int = 10,
    dry_run: bool = False,
    run_day: date | None = None,
    download_posters: bool = True,
    artwork_max_bytes: int = 25 * 1024 * 1024,
    persist_log: bool = True,
    fetch_text: FetchText | None = None,
    seed_provider: str = "javdb",
) -> DailyHotSeedResult:
    """Collect buzz codes, score, resolve via discover waterfall, seed top N."""

    day = run_day or date.today()
    if fetch_text is None:
        if http_client is None:
            raise ValueError("http_client or fetch_text is required")

        async def _fetch(url: str) -> str:
            return await default_fetch_text(http_client, url)

        fetch = _fetch
    else:
        fetch = fetch_text

    mentions, source_statuses = await collect_buzz_mentions(fetch)
    considered = score_mentions(mentions)

    projected: list[HotCandidate] = []
    existing_codes: set[str] = set()
    for candidate in considered:
        work = repo.find_work_by_code(candidate.code)
        if work is None:
            projected.append(candidate)
            continue
        has_media = bool(repo.list_assets_for_work(work.id))
        state = "in_library" if has_media else "catalog_only"
        existing_codes.add(candidate.code)
        projected.append(candidate.model_copy(update={"state": state}))

    targets, skipped = select_seed_targets(
        projected, limit=limit, existing_codes=existing_codes
    )

    seeded: list[SeededWorkSummary] = []
    failures: list[SeedFailure] = []
    # Surface skipped/blocked sources as soft failures in the run log for ops visibility.
    for status in source_statuses:
        if not status.ok:
            failures.append(
                SeedFailure(code=None, title=status.source, error=f"source_skipped: {status.detail}")
            )

    for rank, candidate in enumerate(targets, start=1):
        tags = hot_tags(day, rank)
        if dry_run:
            seeded.append(
                SeededWorkSummary(
                    work_id="dry-run",
                    code=candidate.code,
                    title=candidate.title_hint or candidate.code,
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
                provider=seed_provider,
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
                    code=candidate.code,
                    title=candidate.title_hint,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    result = DailyHotSeedResult(
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


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
