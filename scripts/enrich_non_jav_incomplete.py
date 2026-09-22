#!/usr/bin/env python3
"""Fill missing non-JAV work fields from ThePornDB (REST).

High-value fields: cover, actors (real people only via studio_guard), plot/description,
tags, release_date, rating. Optional CN title/plot via existing translation backends.

Does not invent metadata — skips when TPDB match confidence is low or the source fails.
Never pushes data/ to git. Prefer running against NAS live DB; --sync-nas for local→NAS.

Example::

    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --dry-run --limit 20
    PYTHONPATH=src .venv/bin/python scripts/enrich_non_jav_incomplete.py --limit 200
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
    if not (work.plot or getattr(work, "original_plot", None)):
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

    return score


def build_search_queries(work: Work) -> list[str]:
    queries: list[str] = []
    code = (work.primary_code or "").strip()
    if code and code.upper() not in {"N/A", "NA", "NONE"}:
        queries.append(code)
    title = (work.title or "").strip()
    actors = [a for a in (work.actors or []) if a and classify_non_jav_actor_rejection(a) is None]
    if title and actors:
        queries.append(f"{title} {actors[0]}")
    if title:
        queries.append(title)
    if actors and work.studio:
        queries.append(f"{actors[0]} {work.studio}")
    # unique preserve order
    seen: set[str] = set()
    out: list[str] = []
    for q in queries:
        key = q.casefold()
        if key in seen or len(q) < 3:
            continue
        seen.add(key)
        out.append(q)
    return out[:4]


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

    async def search(self, query: str, *, per_page: int = 5) -> list[dict[str, Any]]:
        try:
            response = await self._client.get(
                _TPDB_SCENES,
                params={"q": query, "per_page": per_page},
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
    for query in build_search_queries(work):
        for row in await tpdb.search(query):
            score = _score_candidate(work, row)
            if score > best_score:
                best_score = score
                best = row
        if best_score >= 4.0:
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
)


def looks_like_placeholder_work(work: Work) -> bool:
    title = work.title or ""
    code = (work.primary_code or "").strip().upper()
    if any(marker in title for marker in _PLACEHOLDER_TITLE_MARKERS):
        return True
    if code.endswith("-01") and any(code.startswith(p) for p in ("TH-", "XK-", "MD-BRAND", "TM-")):
        return True
    # Geographic 探花 channel placeholders without a concrete person
    if "探花" in title and not work.actors:
        return True
    return False


def list_incomplete_non_jav(repo: Repository, *, limit: int | None) -> list[Work]:
    works = [
        work
        for work in repo.list_works()
        if work.family != ContentFamily.JAV.value
        and field_gaps(work)
        and not looks_like_placeholder_work(work)
    ]

    def priority(work: Work) -> tuple[int, int, int, int]:
        gaps = field_gaps(work)
        high = sum(1 for g in gaps if g in {"plot", "actors", "cover", "release_date"})
        # Prefer works with searchable handles (actor name or digit/code)
        searchable = 0
        if work.actors:
            searchable += 2
        code = (work.primary_code or "").strip()
        if code.isdigit() or (code and any(ch.isdigit() for ch in code)):
            searchable += 2
        if work.family == ContentFamily.WESTERN.value:
            searchable += 1
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
    database.initialize()
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
        with database.session() as session:
            targets = list_incomplete_non_jav(Repository(session), limit=arguments.limit)
        print(f"==> non-JAV incomplete targets: {len(targets)}")

        for work_stub in targets:
            stats.scanned += 1
            try:
                with database.session() as session:
                    repo = Repository(session)
                    work = repo.get_work(work_stub.id)
                    if work is None:
                        continue
                    gaps_before = field_gaps(work)
                    if not gaps_before:
                        stats.skipped_complete += 1
                        continue
                    if arguments.dry_run:
                        scene, score = await find_best_scene(tpdb, work)
                        if scene is None:
                            stats.skipped_no_match += 1
                            print(f"[dry] {work.title[:50]!r} gaps={gaps_before} no_match score={score:.2f}")
                        else:
                            stats.matched += 1
                            print(
                                f"[dry] {work.title[:50]!r} gaps={gaps_before} "
                                f"match={scene.get('title')!r} score={score:.2f}"
                            )
                        continue

                    scene, score = await find_best_scene(tpdb, work)
                    if scene is None:
                        stats.skipped_no_match += 1
                        print(f"[skip] {work.primary_code or work.title[:40]!r} no_match score={score:.2f}")
                        continue
                    stats.matched += 1
                    family = ContentFamily(work.family) if work.family in ContentFamily._value2member_map_ else ContentFamily.WESTERN
                    category = MediaCategory(work.category) if work.category in MediaCategory._value2member_map_ else MediaCategory.EUROPE
                    record = scene_to_record(scene, family=family, category=category)
                    # NEVER create new works — only fill gaps on the target row.
                    updated = work
                    sources = dict(updated.field_sources or {})
                    if not (updated.plot or getattr(updated, "original_plot", None)) and record.plot:
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
                    # Persist identity link without creating a second work.
                    try:
                        repo._add_record_identities(updated, record)  # noqa: SLF001
                    except Exception:
                        pass
                    repo._session.flush()
                    updated = repo.get_work(updated.id) or updated

                    gaps_after_meta = field_gaps(updated)
                    for name in ("plot", "actors", "release_date", "tags", "cover", "rating"):
                        if name in gaps_before and name not in gaps_after_meta:
                            stats.filled[name] += 1

                    if not arguments.no_posters and updated.artwork:
                        art_result, local_paths = await ArtworkStore(
                            settings.data_dir / "artwork",
                            client,
                            max_bytes=settings.artwork_max_bytes,
                        ).acquire(updated)
                        if local_paths:
                            repo.update_artwork_local_paths(updated, local_paths)
                        if art_result.downloaded:
                            stats.filled["cover"] += 0  # already counted if cover gap closed
                        updated = repo.get_work(updated.id) or updated

                    if (not arguments.no_translate) and settings.translation_enabled and ("title_cn" in gaps_before or "plot" in gaps_before):
                        try:
                            result = await asyncio.wait_for(translator.translate_work(repo, updated), timeout=25.0)
                            if result.status == "translated":
                                stats.filled["translated"] += 1
                        except TimeoutError:
                            print(f"[warn] translate timeout {updated.id}", flush=True)
                        except Exception as exc:  # noqa: BLE001 - translation is best-effort
                            print(f"[warn] translate {updated.id}: {type(exc).__name__}: {exc}", flush=True)
                        updated = repo.get_work(updated.id) or updated

                    gaps_after = field_gaps(updated)
                    print(
                        f"[ok] score={score:.2f} title={updated.title[:48]!r} "
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
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be >= 1")
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
