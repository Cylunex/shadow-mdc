#!/usr/bin/env python3
"""Fill missing non-JAV work fields from ThePornDB (REST).

High-value fields: cover, actors (real people only via studio_guard), plot/description,
tags, release_date, rating. Optional CN title/plot via existing translation backends.

Does not invent metadata — skips when TPDB match confidence is low or the source fails.
Never pushes data/ to git. Prefer running against NAS live DB; --sync-nas for local→NAS.

Example::

    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --dry-run --limit 20
    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --limit 200
    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --family western --limit 120
    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --studio-contains "Model Media" --limit 80
    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --sync-nas
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import subprocess
import sys
import unicodedata
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.models import Work  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.domain import Artwork, ProviderRecord  # noqa: E402
from shadow_mdc.enums import ContentFamily, MediaCategory  # noqa: E402
from shadow_mdc.media.artwork import ArtworkStore  # noqa: E402
from shadow_mdc.services.javranking_client import BROWSER_UA  # noqa: E402
from shadow_mdc.services.studio_guard import classify_non_jav_actor_rejection  # noqa: E402
from shadow_mdc.services.translation import (  # noqa: E402
    GoogleTitleTranslator,
    TranslationCache,
    build_translation_backends,
)

try:
    from actor_avatars import theporndb_token_from_env  # type: ignore
except ImportError:  # pragma: no cover
    sys.path.insert(0, str(ROOT / "scripts"))
    from actor_avatars import theporndb_token_from_env  # noqa: E402

_TPDB_SCENES = "https://api.theporndb.net/scenes"
_COLLAPSE = re.compile(r"[^a-z0-9\u4e00-\u9fff]+")

# TPDB REST `site=` short_name hints keyed by studio / title markers.
_STUDIO_SITE_HINTS: tuple[tuple[str, str], ...] = (
    ("model media asia", "modelmediaasia"),
    ("model media us", "modelmediaus"),
    ("model media", "modelmediaasia"),
    ("麻豆", "modelmediaasia"),
    ("madou", "modelmediaasia"),
    ("jules jordan", "julesjordan"),
    ("vixen", "vixen"),
    ("tushy", "tushy"),
    ("blacked", "blacked"),
    ("deeper", "deeper"),
    ("bangbros", "bangbros"),
    ("evil angel", "evilangel"),
    ("digital playground", "digitalplayground"),
    ("lubed", "lubed"),
    ("onlyfans", "onlyfans"),
)

_SWAG_HANDLE_RE = re.compile(r"@([A-Za-z0-9_]{2,32})", re.I)
_BRACKET_CODE_RE = re.compile(r"\[\s*([A-Za-z]{1,10}[-_]?\d{2,6})\s*\]")
_INLINE_CODE_RE = re.compile(r"\b([A-Z]{1,10}-\d{2,6})\b")
_SQLITE_LOCK_MARKERS = ("database is locked", "database table is locked", "database is busy")


def extract_scene_codes(*parts: str | None) -> list[str]:
    """Pull Madou/SWAG-style scene codes from title/code blobs."""

    found: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        for match in _BRACKET_CODE_RE.finditer(part):
            code = match.group(1).upper().replace("_", "-")
            if code not in seen:
                seen.add(code)
                found.append(code)
        for match in _INLINE_CODE_RE.finditer(part.upper()):
            code = match.group(1)
            if code not in seen:
                seen.add(code)
                found.append(code)
    return found


def resolve_tpdb_sites(work: Work) -> list[str]:
    """Best-effort TPDB site short_names for a work (may be empty)."""

    sites: list[str] = []
    studio = (work.studio or "").strip()
    title = work.title or ""
    blob = f"{studio} {title}".casefold()

    if studio.lower().startswith("swaglive:"):
        handle = studio.split(":", 1)[1].strip().casefold()
        handle = re.sub(r"[^a-z0-9]", "", handle)
        if handle:
            sites.append(f"swaglive{handle}")
    for match in _SWAG_HANDLE_RE.finditer(title):
        handle = re.sub(r"[^a-z0-9]", "", match.group(1).casefold())
        if handle:
            sites.append(f"swaglive{handle}")
    if "swag" in blob and not any(s.startswith("swaglive") for s in sites):
        # Broad SWAG query still helps when creator handle is unknown.
        pass

    for marker, short in _STUDIO_SITE_HINTS:
        if marker in blob:
            sites.append(short)

    # unique preserve order
    out: list[str] = []
    seen: set[str] = set()
    for site in sites:
        if site and site not in seen:
            seen.add(site)
            out.append(site)
    return out[:4]


def plot_is_meaningful(work: Work) -> bool:
    """Reject empty or title-echo plots that prior seeds left behind."""

    plot = ((work.plot or "") or (getattr(work, "original_plot", None) or "")).strip()
    if not plot:
        return False
    title = (work.title or "").strip()
    if not title:
        return True
    if collapse_key(plot) == collapse_key(title):
        return False
    # Truncated title dump used as plot (common for early SWAG seeds).
    if len(plot) < 80 and collapse_key(plot) in collapse_key(title):
        return False
    if len(plot) < 24:
        return False
    return True


def with_sqlite_retry(operation, *, attempts: int = 8, base_delay: float = 0.4):
    """Retry a callable when SQLite reports a lock (parallel JAV enrich)."""

    import time

    last_exc: BaseException | None = None
    for attempt in range(max(1, attempts)):
        try:
            return operation()
        except Exception as exc:  # noqa: BLE001 - narrow by message
            message = str(exc).casefold()
            if not any(marker in message for marker in _SQLITE_LOCK_MARKERS):
                raise
            last_exc = exc
            time.sleep(base_delay * (1.6 ** attempt))
    assert last_exc is not None
    raise last_exc



@dataclass
class EnrichStats:
    scanned: int = 0
    matched: int = 0
    skipped_no_match: int = 0
    skipped_complete: int = 0
    failed: int = 0
    filled: dict[str, int] = field(
        default_factory=lambda: {
            "plot": 0,
            "actors": 0,
            "release_date": 0,
            "tags": 0,
            "cover": 0,
            "rating": 0,
            "translated": 0,
        }
    )
    failures: list[dict[str, str]] = field(default_factory=list)


def collapse_key(value: str) -> str:
    return _COLLAPSE.sub("", unicodedata.normalize("NFKC", value).casefold())


def has_cjk(value: str | None) -> bool:
    return bool(value) and any("\u4e00" <= ch <= "\u9fff" for ch in value)


def field_gaps(work: Work) -> list[str]:
    gaps: list[str] = []
    if not plot_is_meaningful(work):
        gaps.append("plot")
    if not work.actors:
        gaps.append("actors")
    if not work.release_date:
        gaps.append("release_date")
    if not work.tags:
        gaps.append("tags")
    art = work.artwork if isinstance(work.artwork, list) else []
    if not art:
        gaps.append("cover")
    if work.rating_value is None:
        gaps.append("rating")
    if not has_cjk(work.title):
        gaps.append("title_cn")
    return gaps


def _performer_names(row: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in row.get("performers") or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        parent = item.get("parent") if isinstance(item.get("parent"), dict) else None
        preferred = None
        if parent:
            preferred = parent.get("full_name") or parent.get("name")
        preferred = preferred or name
        if not isinstance(preferred, str):
            continue
        cleaned = preferred.strip()
        if not cleaned:
            continue
        if classify_non_jav_actor_rejection(cleaned) is not None:
            continue
        if cleaned not in names:
            names.append(cleaned)
    return names


def _scene_image_url(row: dict[str, Any]) -> str | None:
    for key in ("poster", "image", "background"):
        value = row.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
        if isinstance(value, dict):
            for size in ("large", "full", "medium", "small"):
                nested = value.get(size)
                if isinstance(nested, str) and nested.startswith("http"):
                    return nested
    posters = row.get("posters")
    if isinstance(posters, list):
        for item in posters:
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url.startswith("http"):
                    return url
    return None


def _scene_tags(row: dict[str, Any]) -> list[str]:
    tags: list[str] = []
    for item in row.get("tags") or []:
        if isinstance(item, dict):
            name = item.get("name")
        elif isinstance(item, str):
            name = item
        else:
            name = None
        if isinstance(name, str) and name.strip() and name.strip() not in tags:
            tags.append(name.strip())
    return tags


def _parse_date(raw: object) -> date | None:
    if not isinstance(raw, str) or len(raw) < 10:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def _score_candidate(work: Work, row: dict[str, Any]) -> float:
    """Conservative match score; require >= 2.5 to accept."""

    score = 0.0
    title = str(row.get("title") or "")
    work_title = work.title or ""
    wt = collapse_key(work_title)
    rt = collapse_key(title)
    if wt and rt:
        if wt == rt:
            score += 3.0
        elif wt in rt or rt in wt:
            score += 1.5
        else:
            # token overlap
            w_tokens = set(re.findall(r"[a-z0-9\u4e00-\u9fff]{3,}", work_title.casefold()))
            r_tokens = set(re.findall(r"[a-z0-9\u4e00-\u9fff]{3,}", title.casefold()))
            if w_tokens and r_tokens:
                overlap = len(w_tokens & r_tokens) / max(len(w_tokens), 1)
                score += overlap * 2.0

    code = (work.primary_code or "").strip()
    if code:
        code_key = collapse_key(code)
        blob = collapse_key(
            " ".join(
                str(x)
                for x in (
                    title,
                    row.get("sku"),
                    row.get("external_id"),
                    (row.get("site") or {}).get("name") if isinstance(row.get("site"), dict) else "",
                )
                if x
            )
        )
        if code_key and code_key in blob:
            score += 2.5

    work_actors = {collapse_key(a) for a in (work.actors or []) if a}
    scene_actors = {collapse_key(a) for a in _performer_names(row)}
    if work_actors and scene_actors and work_actors & scene_actors:
        score += 1.5

    studio = (work.studio or "").strip()
    site = row.get("site") if isinstance(row.get("site"), dict) else {}
    site_name = str(site.get("name") or "")
    if studio and site_name and collapse_key(studio) in collapse_key(site_name):
        score += 0.75
    elif studio and site_name and collapse_key(site_name) in collapse_key(studio):
        score += 0.75

    expected_sites = set(resolve_tpdb_sites(work))
    site_short = str(site.get("short_name") or "").casefold()
    if expected_sites and site_short and site_short in expected_sites:
        score += 1.25

    # Exact scene-code hit in title/sku
    for code in extract_scene_codes(work.primary_code, work.title):
        blob = collapse_key(
            " ".join(
                str(x)
                for x in (title, row.get("sku"), row.get("external_id"), row.get("title"))
                if x
            )
        )
        if collapse_key(code) and collapse_key(code) in blob:
            score += 2.0
            break

    return score


def build_search_queries(work: Work) -> list[str]:
    queries: list[str] = []
    for code in extract_scene_codes(work.primary_code, work.title):
        queries.append(code)
    code = (work.primary_code or "").strip()
    if code and code.upper() not in {"N/A", "NA", "NONE"} and len(code) >= 3:
        # Skip Mongo-ish seed ids; prefer human codes.
        if not re.fullmatch(r"[0-9a-f]{16,}", code.casefold()):
            queries.append(code)
    title = (work.title or "").strip()
    # Strip trailing "| Swag @handle video" noise for cleaner TPDB search.
    title_clean = re.sub(r"\s*[|｜].*$", "", title).strip()
    title_clean = re.sub(r"\s*\|?\s*Swag\s*@\S+.*$", "", title_clean, flags=re.I).strip()
    actors = [a for a in (work.actors or []) if a and classify_non_jav_actor_rejection(a) is None]
    if title_clean and actors:
        # Prefer short title + actor (TPDB ranking is sensitive to extra tokens).
        short = title_clean
        if len(short) > 80:
            short = short[:80].rsplit(" ", 1)[0]
        queries.append(f"{short} {actors[0]}")
        queries.append(f"{actors[0]} {short}")
    if title_clean:
        queries.append(title_clean)
    if actors and work.studio:
        studio = work.studio
        if studio.casefold().startswith("swaglive:"):
            studio = studio.split(":", 1)[1].strip() or studio
        queries.append(f"{actors[0]} {studio}")
    if actors and work.family == ContentFamily.WESTERN.value:
        queries.append(actors[0])
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.casefold()
        if key in seen or len(q) < 3:
            continue
        seen.add(key)
        out.append(q)
    return out[:6]


def scene_to_record(row: dict[str, Any], *, family: ContentFamily, category: MediaCategory) -> ProviderRecord:
    external_id = str(row.get("id") or row.get("_id") or "")
    title = str(row.get("title") or "").strip()
    plot = (row.get("description") or row.get("details") or "")
    plot_text = plot.strip() if isinstance(plot, str) else None
    actors = tuple(_performer_names(row))
    tags = tuple(_scene_tags(row))
    image = _scene_image_url(row)
    artwork = (Artwork(url=image, kind="poster"),) if image else ()
    site = row.get("site") if isinstance(row.get("site"), dict) else {}
    studio = str(site.get("name") or "").strip() or None
    rating = row.get("rating")
    rating_value = float(rating) if isinstance(rating, (int, float)) else None
    duration = row.get("duration")
    runtime = int(duration) if isinstance(duration, (int, float)) and duration else None
    code = None
    sku = row.get("sku") or row.get("external_id")
    if isinstance(sku, str) and sku.strip():
        # Prefer trailing code-like token (MD-0265)
        m = re.search(r"\b([A-Z]{1,10}-?\d{2,6})\b", sku.upper())
        if m:
            code = m.group(1)
        elif re.fullmatch(r"\d{2,6}", sku.strip()):
            code = sku.strip()
    return ProviderRecord(
        provider="theporndb",
        external_id=external_id or title,
        source_url=f"https://theporndb.net/scenes/{external_id}" if external_id else None,
        code=code,
        title=title or external_id,
        family=family,
        category=category,
        release_date=_parse_date(row.get("date")),
        runtime_seconds=runtime,
        studio=studio,
        plot=plot_text or None,
        actors=actors,
        tags=tags,
        artwork=artwork,
        language="en",
        rating=rating_value,
        rating_max=5.0 if rating_value is not None else None,
    )


class TpdbClient:
    def __init__(self, client: httpx.AsyncClient, token: str):
        self._client = client
        self._token = token
        self._detail_cache: dict[str, dict[str, Any]] = {}

    async def search(
        self, query: str, *, per_page: int = 5, site: str | None = None
    ) -> list[dict[str, Any]]:
        params: dict[str, object] = {"q": query, "per_page": per_page}
        if site:
            params["site"] = site
        try:
            response = await self._client.get(
                _TPDB_SCENES,
                params=params,
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            return []
        if response.status_code != 200:
            return []
        try:
            rows = response.json().get("data") or []
        except ValueError:
            return []
        return [row for row in rows if isinstance(row, dict)]

    async def detail(self, scene_id: str) -> dict[str, Any] | None:
        if scene_id in self._detail_cache:
            return self._detail_cache[scene_id]
        try:
            response = await self._client.get(
                f"{_TPDB_SCENES}/{scene_id}",
                headers={"Authorization": f"Bearer {self._token}"},
            )
        except httpx.HTTPError:
            return None
        if response.status_code != 200:
            return None
        try:
            payload = response.json()
        except ValueError:
            return None
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            return None
        self._detail_cache[scene_id] = data
        return data


async def find_best_scene(tpdb: TpdbClient, work: Work) -> tuple[dict[str, Any] | None, float]:
    best: dict[str, Any] | None = None
    best_score = 0.0
    sites = resolve_tpdb_sites(work)
    queries = build_search_queries(work)
    # Prefer fewer queries when we already have a site hint.
    if sites:
        queries = queries[:3]
    # Site-scoped passes first (much higher precision for Model Media / SWAGLive).
    # Only fall back to unscoped search when site passes stay weak.
    passes: list[str | None] = list(sites) if sites else [None]
    if sites:
        passes = passes[:2]
    seen_pass: set[str | None] = set()
    for site in passes:
        if site in seen_pass:
            continue
        seen_pass.add(site)
        for query in queries:
            for row in await tpdb.search(query, site=site, per_page=3):
                score = _score_candidate(work, row)
                if score > best_score:
                    best_score = score
                    best = row
            # Accept a solid site-scoped hit without burning more queries.
            if site is not None and best_score >= 3.0:
                break
            if best_score >= 4.0:
                break
        if site is not None and best_score >= 3.0:
            break
        if best_score >= 4.0:
            break
    if best_score < 2.5 and sites:
        for query in queries[:2]:
            for row in await tpdb.search(query, per_page=3):
                score = _score_candidate(work, row)
                if score > best_score:
                    best_score = score
                    best = row
            if best_score >= 3.5:
                break
    if best is None or best_score < 2.5:
        return None, best_score
    scene_id = str(best.get("id") or "")
    if scene_id:
        detailed = await tpdb.detail(scene_id)
        if detailed is not None:
            return detailed, best_score
    return best, best_score


def _maybe_sync_nas(*, data_dir: Path, dry_run: bool) -> int:
    script = ROOT / "scripts" / "sync_catalog_to_nas.sh"
    if not script.is_file():
        print(f"sync script missing: {script}", file=sys.stderr)
        return 2
    env = os.environ.copy()
    env["SOURCE_DATA_DIR"] = str(data_dir)
    cmd = [str(script)]
    if dry_run:
        cmd.append("--dry-run")
    print(f"==> post-enrich NAS sync: {' '.join(cmd)}")
    return int(subprocess.run(cmd, cwd=str(ROOT), env=env, check=False).returncode)


_PLACEHOLDER_TITLE_MARKERS = (
    "精选",
    "样例",
    "合集",
    "厂牌",
    "度假主题",
    "经典场次",
    "约会记录",
    "居家日常",
    "校园邂逅",
    "都市情感",
    "职场主题",
    "闺蜜主题",
)


def looks_like_placeholder_work(work: Work) -> bool:
    title = work.title or ""
    code = (work.primary_code or "").strip().upper()
    if any(marker in title for marker in _PLACEHOLDER_TITLE_MARKERS):
        return True
    if code.endswith(("-01", "-02")) and any(
        code.startswith(p) for p in ("TH-", "XK-", "MD-BRAND", "TM-", "MD-", "MDX-", "SWAG-CN")
    ):
        # Brand / actor "精选" seed rows (MD-SUCHANG-01, SWAG-CN-01…).
        if any(marker in title for marker in ("精选", "主题", "华语创作者")) or re.fullmatch(
            r"(?:MD|MDX|SWAG-CN|TH|XK|TM)-[A-Z0-9]+-0[12]", code
        ):
            return True
    if code.endswith("-01") and any(code.startswith(p) for p in ("TH-", "XK-", "MD-BRAND", "TM-")):
        return True
    # Geographic 探花 channel placeholders without a concrete person
    if "探花" in title and not work.actors:
        return True
    return False


def list_incomplete_non_jav(
    repo: Repository,
    *,
    limit: int | None,
    family: str | None = None,
    studio_contains: str | None = None,
) -> list[Work]:
    family_key = family.casefold().strip() if family else None
    studio_key = studio_contains.casefold().strip() if studio_contains else None
    works = []
    for work in repo.list_works():
        if work.family == ContentFamily.JAV.value:
            continue
        if family_key and (work.family or "").casefold() != family_key:
            continue
        if studio_key and studio_key not in (work.studio or "").casefold():
            continue
        if not field_gaps(work):
            continue
        if looks_like_placeholder_work(work):
            continue
        works.append(work)

    def priority(work: Work) -> tuple[int, int, int, int]:
        gaps = field_gaps(work)
        high = sum(1 for g in gaps if g in {"plot", "actors", "cover", "release_date"})
        # Prefer works with searchable handles (actor name or digit/code)
        searchable = 0
        if work.actors:
            searchable += 2
        codes = extract_scene_codes(work.primary_code, work.title)
        if codes:
            searchable += 3
        code = (work.primary_code or "").strip()
        if code.isdigit() or (code and any(ch.isdigit() for ch in code)):
            searchable += 2
        if work.family == ContentFamily.WESTERN.value:
            searchable += 2
        if resolve_tpdb_sites(work):
            searchable += 2
        if work.studio and work.studio not in {"探花", "91探花"}:
            searchable += 1
        return (-searchable, -high, len(gaps), 0)

    works.sort(key=priority)
    if limit is not None:
        works = works[: max(1, limit)]
    return works


async def _run(arguments: argparse.Namespace) -> int:
    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    if arguments.proxy:
        updates["proxy_url"] = arguments.proxy
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    token = arguments.token or theporndb_token_from_env(ROOT / ".env") or settings.theporndb_token
    if not token:
        print("SHADOW_MDC_THEPORNDB_TOKEN missing", file=sys.stderr)
        return 3

    database = Database(settings.database_url)

    def _init_db() -> None:
        # Full migrate can block behind parallel JAV enrich writers; prefer light path.
        if os.environ.get("SHADOW_MDC_ENRICH_LIGHT_INIT", "").strip().lower() in {
            "1",
            "true",
            "yes",
        }:
            with database.engine.begin() as connection:
                if database.engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            return
        database.initialize()  # already sets sqlite busy_timeout=30000

    with_sqlite_retry(_init_db, attempts=16, base_delay=0.6)
    stats = EnrichStats()

    client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": BROWSER_UA, "Accept": "application/json"},
        proxy=arguments.proxy if arguments.proxy else settings.proxy_url,
    )
    tpdb = TpdbClient(client, token)
    backends = build_translation_backends(
        client,
        google_endpoint=settings.translation_endpoint,
        deepl_api_url=settings.translation_deepl_api_url,
        deepl_api_key=settings.translation_deepl_api_key or settings.translation_api_key,
        deeplx_endpoint=settings.translation_deeplx_endpoint,
        custom_endpoint=settings.translation_custom_endpoint,
        prefer=settings.translation_backend,
    )
    translator = GoogleTitleTranslator(
        client,
        TranslationCache(settings.data_dir / "translations.db"),
        enabled=settings.translation_enabled and not arguments.no_translate,
        endpoint=settings.translation_endpoint,
        target_language=settings.translation_target_language,
        extra_backends=[b for b in backends if b.name != "google"],
        translate_plot=settings.translation_plot,
    )

    try:
        def _load_targets():
            with database.session() as session:
                return list_incomplete_non_jav(
                    Repository(session),
                    limit=arguments.limit,
                    family=arguments.family,
                    studio_contains=arguments.studio_contains,
                )

        targets = with_sqlite_retry(_load_targets)
        print(
            f"==> non-JAV incomplete targets: {len(targets)}"
            + (f" family={arguments.family}" if arguments.family else "")
            + (f" studio~={arguments.studio_contains}" if arguments.studio_contains else "")
        )

        for work_stub in targets:
            stats.scanned += 1
            try:
                # Phase 1: short read — detach work fields before any HTTP.
                def _read():
                    with database.session() as session:
                        work = Repository(session).get_work(work_stub.id)
                        if work is None:
                            return None
                        # expire-safe snapshot
                        return {
                            "id": work.id,
                            "title": work.title,
                            "primary_code": work.primary_code,
                            "family": work.family,
                            "category": work.category,
                            "studio": work.studio,
                            "actors": list(work.actors or []),
                            "tags": list(work.tags or []),
                            "plot": work.plot,
                            "original_plot": getattr(work, "original_plot", None),
                            "release_date": work.release_date,
                            "rating_value": work.rating_value,
                            "artwork": list(work.artwork or []) if isinstance(work.artwork, list) else [],
                            "field_sources": dict(work.field_sources or {}),
                            "gaps": field_gaps(work),
                        }

                snap = with_sqlite_retry(_read)
                if snap is None:
                    continue
                gaps_before = snap["gaps"]
                if not gaps_before:
                    stats.skipped_complete += 1
                    continue
                work_view = SimpleNamespace(
                    id=snap["id"],
                    title=snap["title"],
                    primary_code=snap["primary_code"],
                    family=snap["family"],
                    category=snap["category"],
                    studio=snap["studio"],
                    actors=snap["actors"],
                    tags=snap["tags"],
                    plot=snap["plot"],
                    original_plot=snap["original_plot"],
                    release_date=snap["release_date"],
                    rating_value=snap["rating_value"],
                    artwork=snap["artwork"],
                    field_sources=snap["field_sources"],
                )

                # Phase 2: network outside any DB session.
                scene, score = await find_best_scene(tpdb, work_view)  # type: ignore[arg-type]
                if arguments.dry_run:
                    if scene is None:
                        stats.skipped_no_match += 1
                        print(f"[dry] {(snap['title'] or '')[:50]!r} gaps={gaps_before} no_match score={score:.2f}")
                    else:
                        stats.matched += 1
                        print(
                            f"[dry] {(snap['title'] or '')[:50]!r} gaps={gaps_before} "
                            f"match={scene.get('title')!r} score={score:.2f}"
                        )
                    continue
                if scene is None:
                    stats.skipped_no_match += 1
                    print(f"[skip] {snap['primary_code'] or (snap['title'] or '')[:40]!r} no_match score={score:.2f}")
                    continue

                family = (
                    ContentFamily(snap["family"])
                    if snap["family"] in ContentFamily._value2member_map_
                    else ContentFamily.WESTERN
                )
                category = (
                    MediaCategory(snap["category"])
                    if snap["category"] in MediaCategory._value2member_map_
                    else MediaCategory.EUROPE
                )
                record = scene_to_record(scene, family=family, category=category)

                # Phase 3: short write transaction only.
                def _write():
                    with database.session() as session:
                        repo = Repository(session)
                        updated = repo.get_work(snap["id"])
                        if updated is None:
                            return None, gaps_before, gaps_before
                        gaps_now = field_gaps(updated)
                        sources = dict(updated.field_sources or {})
                        if not plot_is_meaningful(updated) and record.plot:
                            repo.update_work_fields(updated, plot=record.plot, lock_edited=False)
                            updated = repo.get_work(updated.id) or updated
                            sources = dict(updated.field_sources or {})
                            sources["plot"] = "theporndb"
                            updated.field_sources = sources
                        if not updated.actors and record.actors:
                            repo.update_work_fields(updated, actors=list(record.actors), lock_edited=False)
                            updated = repo.get_work(updated.id) or updated
                        if not updated.tags and record.tags:
                            repo.update_work_fields(updated, tags=list(record.tags), lock_edited=False)
                            updated = repo.get_work(updated.id) or updated
                        if updated.release_date is None and record.release_date is not None:
                            updated.release_date = record.release_date
                            sources = dict(updated.field_sources or {})
                            sources["release_date"] = "theporndb"
                            updated.field_sources = sources
                        if updated.rating_value is None and record.rating is not None and float(record.rating) > 0:
                            updated.rating_value = float(record.rating)
                            updated.rating_max = float(record.rating_max) if record.rating_max else 5.0
                            updated.rating_source = "theporndb"
                            sources = dict(updated.field_sources or {})
                            sources["rating"] = "theporndb"
                            updated.field_sources = sources
                        if (not updated.artwork) and record.artwork:
                            updated.artwork = [
                                {"url": item.url, "kind": item.kind, "source": "theporndb"}
                                for item in record.artwork
                                if item.url
                            ]
                            sources = dict(updated.field_sources or {})
                            sources["artwork"] = "theporndb"
                            updated.field_sources = sources
                        try:
                            repo._add_record_identities(updated, record)  # noqa: SLF001
                        except Exception:
                            pass
                        repo._session.flush()
                        updated = repo.get_work(updated.id) or updated
                        return updated, gaps_now, field_gaps(updated)

                updated, gaps_meta_before, gaps_after_meta = with_sqlite_retry(_write, attempts=10, base_delay=0.5)
                if updated is None:
                    continue
                stats.matched += 1
                for name in ("plot", "actors", "release_date", "tags", "cover", "rating"):
                    if name in gaps_before and name not in gaps_after_meta:
                        stats.filled[name] += 1

                # Phase 4: optional artwork/translate in their own short sessions.
                if not arguments.no_posters and updated.artwork:
                    art_result, local_paths = await ArtworkStore(
                        settings.data_dir / "artwork",
                        client,
                        max_bytes=settings.artwork_max_bytes,
                    ).acquire(updated)

                    def _art_write():
                        with database.session() as session:
                            repo = Repository(session)
                            current = repo.get_work(updated.id)
                            if current is None or not local_paths:
                                return current
                            repo.update_artwork_local_paths(current, local_paths)
                            return repo.get_work(current.id)

                    updated = with_sqlite_retry(_art_write) or updated

                if (
                    (not arguments.no_translate)
                    and settings.translation_enabled
                    and ("title_cn" in gaps_before or "plot" in gaps_before)
                ):
                    try:
                        def _translate():
                            with database.session() as session:
                                repo = Repository(session)
                                current = repo.get_work(updated.id)
                                if current is None:
                                    return None
                                return current, repo

                        pair = with_sqlite_retry(_translate)
                        if pair is not None:
                            current, repo = pair
                            result = await asyncio.wait_for(
                                translator.translate_work(repo, current), timeout=25.0
                            )
                            if result.status == "translated":
                                stats.filled["translated"] += 1
                            # session closes after translate_work may have written via repo
                    except TimeoutError:
                        print(f"[warn] translate timeout {updated.id}", flush=True)
                    except Exception as exc:  # noqa: BLE001
                        print(f"[warn] translate {updated.id}: {type(exc).__name__}: {exc}", flush=True)

                    def _reload():
                        with database.session() as session:
                            return Repository(session).get_work(updated.id)

                    updated = with_sqlite_retry(_reload) or updated

                gaps_after = field_gaps(updated)
                print(
                    f"[ok] score={score:.2f} title={(updated.title or '')[:48]!r} "
                    f"filled_closed={sorted(set(gaps_before) - set(gaps_after))} remain={gaps_after}",
                    flush=True,
                )
                await asyncio.sleep(float(arguments.delay))
            except Exception as exc:  # noqa: BLE001
                stats.failed += 1
                stats.failures.append({"id": work_stub.id, "error": f"{type(exc).__name__}: {exc}"})
                print(f"[fail] {work_stub.id} {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)
    finally:
        await client.aclose()

    summary = {
        "scanned": stats.scanned,
        "matched": stats.matched,
        "skipped_no_match": stats.skipped_no_match,
        "skipped_complete": stats.skipped_complete,
        "failed": stats.failed,
        "filled": stats.filled,
        "failures": stats.failures[:20],
        "dry_run": arguments.dry_run,
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    sync_rc = 0
    want_sync = arguments.sync_nas or os.environ.get("SHADOW_MDC_SYNC_NAS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if want_sync and not arguments.dry_run:
        sync_rc = _maybe_sync_nas(data_dir=settings.data_dir, dry_run=False)
    elif want_sync and arguments.dry_run:
        print("skipping --sync-nas because this was a dry-run")
    return sync_rc if sync_rc else (1 if stats.failed and stats.matched == 0 else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--token", default=None, help="TPDB token (else env / .env)")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--no-posters", action="store_true")
    parser.add_argument("--sync-nas", action="store_true")
    parser.add_argument("--delay", type=float, default=0.35, help="seconds between TPDB lookups")
    parser.add_argument(
        "--family",
        default=None,
        help="only this content family (chinese/western/korean/unknown)",
    )
    parser.add_argument(
        "--studio-contains",
        default=None,
        help="substring filter on works.studio (e.g. 'Model Media', 'SWAG')",
    )
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be >= 1")
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
