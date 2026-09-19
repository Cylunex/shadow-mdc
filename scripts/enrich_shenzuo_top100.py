#!/usr/bin/env python3
"""Fully enrich 神作 TOP100 works into the local catalog.

Idempotent: seeds missing codes, refreshes unlocked metadata from providers,
translates title/plot when enabled, downloads artwork. Prefer running on the
box (providers often reachable without proxy); if metadata fetch fails, re-run
with ``SHADOW_MDC_PROXY_URL=http://192.168.0.110:7893`` or via SSH on NAS with
proxy for metadata only (do not scrape historical rankings on NAS).

Example::

    PYTHONPATH=src .venv/bin/python scripts/enrich_shenzuo_top100.py --dry-run
    PYTHONPATH=src .venv/bin/python scripts/enrich_shenzuo_top100.py --limit 100
    PYTHONPATH=src .venv/bin/python scripts/enrich_shenzuo_top100.py --sync-nas
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.models import Work  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.domain import IdentityHints  # noqa: E402
from shadow_mdc.enums import MatchDecision, MediaCategory, QueryMode  # noqa: E402
from shadow_mdc.identity import extract_code  # noqa: E402
from shadow_mdc.matching import rank_candidates  # noqa: E402
from shadow_mdc.media.artwork import ArtworkStore  # noqa: E402
from shadow_mdc.normalize_code import normalize_code, to_comparison_key  # noqa: E402
from shadow_mdc.providers.airav import AirAvProvider  # noqa: E402
from shadow_mdc.providers.avsox import AvSoxProvider  # noqa: E402
from shadow_mdc.providers.base import ProviderRegistry  # noqa: E402
from shadow_mdc.providers.fanza import FanzaProvider  # noqa: E402
from shadow_mdc.providers.freejavbt import FreeJavBtProvider  # noqa: E402
from shadow_mdc.providers.jav321 import Jav321Provider  # noqa: E402
from shadow_mdc.providers.javbus import JavBusProvider  # noqa: E402
from shadow_mdc.providers.javdb import JavDBProvider  # noqa: E402
from shadow_mdc.providers.javlibrary import JavLibraryProvider  # noqa: E402
from shadow_mdc.providers.r18dev import R18DevProvider  # noqa: E402
from shadow_mdc.services.discover import DiscoverService  # noqa: E402
from shadow_mdc.services.javranking_client import BROWSER_UA  # noqa: E402
from shadow_mdc.services.daily_chart_seed import merge_tags  # noqa: E402
from shadow_mdc.services.translation import (  # noqa: E402
    GoogleTitleTranslator,
    TranslationCache,
    build_translation_backends,
)

LIST_PATH = ROOT / "data" / "javranking" / "list-most-awarded-videos.json"
SHENZUO_TAGS = ("javranking", "most-awarded-videos", "shenzuo")

TRACK_FIELDS = (
    "title",
    "original_title",
    "actors",
    "release_date",
    "runtime_seconds",
    "directors",
    "studio",
    "series",
    "rating_value",
    "category",
    "tags",
    "plot",
    "original_plot",
    "artwork",
)


@dataclass(frozen=True)
class TopEntry:
    position: int
    code: str
    title: str
    video_id: int | None
    url: str | None


def _http_client(settings: Settings, *, max_connections: int, proxy: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "ja,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        proxy=proxy if proxy is not None else settings.proxy_url,
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=0,
        ),
    )


def load_top100(path: Path) -> list[TopEntry]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    videos = payload.get("videos") or payload.get("items") or []
    out: list[TopEntry] = []
    for item in videos:
        raw = (item.get("code") or "").strip()
        code = normalize_code(raw) or raw
        if not code:
            continue
        out.append(
            TopEntry(
                position=int(item.get("position") or len(out) + 1),
                code=code,
                title=str(item.get("title") or code),
                video_id=item.get("video_id"),
                url=item.get("url"),
            )
        )
    return out


def field_present(work: Work, name: str) -> bool:
    if name == "title":
        return bool((work.title or "").strip())
    if name == "original_title":
        return bool((getattr(work, "original_title", None) or "").strip())
    if name == "actors":
        return bool(work.actors)
    if name == "release_date":
        return bool(work.release_date)
    if name == "runtime_seconds":
        return bool(work.runtime_seconds)
    if name == "directors":
        return bool(work.directors)
    if name == "studio":
        return bool(work.studio)
    if name == "series":
        return bool(work.series)
    if name == "rating_value":
        return work.rating_value is not None
    if name == "category":
        return bool(work.category)
    if name == "tags":
        return bool(work.tags)
    if name == "plot":
        return bool((work.plot or "").strip())
    if name == "original_plot":
        return bool((getattr(work, "original_plot", None) or "").strip())
    if name == "artwork":
        art = work.artwork if isinstance(work.artwork, list) else []
        return len(art) > 0
    return False


def completeness_stats(works_by_code: dict[str, Work], entries: list[TopEntry]) -> dict[str, Any]:
    cataloged = 0
    fills = {name: 0 for name in TRACK_FIELDS}
    gaps: list[dict[str, Any]] = []
    for entry in entries:
        key = to_comparison_key(entry.code)
        work = works_by_code.get(key)
        if work is None:
            gaps.append({"position": entry.position, "code": entry.code, "missing": ["NOT_IN_DB"]})
            continue
        cataloged += 1
        missing_fields: list[str] = []
        for name in TRACK_FIELDS:
            if field_present(work, name):
                fills[name] += 1
            else:
                missing_fields.append(name)
        if missing_fields:
            gaps.append({"position": entry.position, "code": entry.code, "missing": missing_fields})
    return {
        "total": len(entries),
        "cataloged": cataloged,
        "fills": {
            name: {
                "count": fills[name],
                "pct": round(100.0 * fills[name] / max(cataloged, 1), 1),
            }
            for name in TRACK_FIELDS
        },
        "incomplete_or_missing": len(gaps),
        "gap_sample": gaps[:20],
    }


def index_works(repo: Repository) -> dict[str, Work]:
    by_key: dict[str, Work] = {}
    for work in repo.list_works():
        if not work.primary_code:
            continue
        key = to_comparison_key(normalize_code(work.primary_code) or work.primary_code)
        if key:
            by_key[key] = work
    return by_key


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
    completed = subprocess.run(cmd, cwd=str(ROOT), env=env, check=False)
    return int(completed.returncode)


def needs_refresh(work: Work) -> bool:
    required = ("actors", "studio", "release_date", "plot", "original_plot")  # runtime/rating optional
    return any(not field_present(work, name) for name in required)


async def refresh_work_metadata(
    *,
    providers: ProviderRegistry,
    repo: Repository,
    work: Work,
) -> Work:
    code_raw = work.primary_code or ""
    code, family = extract_code(code_raw, MediaCategory(work.category) if work.category else MediaCategory.JAPAN)
    if code is None:
        return work
    hints = IdentityHints(
        term=code,
        mode=QueryMode.CODE,
        family=family,
        category=MediaCategory(work.category) if work.category else MediaCategory.JAPAN,
        code=code,
    )
    batch = await providers.search(hints)
    ranked = rank_candidates(hints, list(batch.records))
    updated = work
    for candidate in ranked:
        if candidate.decision is not MatchDecision.ACCEPT:
            continue
        updated = repo.upsert_provider_record(candidate.record, overwrite=True)
    return updated


async def ensure_seeded(
    *,
    discover: DiscoverService,
    repo: Repository,
    entry: TopEntry,
    dry_run: bool,
    use_javdb: bool = False,
    seed_timeout: float = 25.0,
) -> tuple[Work | None, str]:
    existing = repo.find_work_by_code(entry.code)
    if existing is not None:
        return existing, "exists"
    if dry_run:
        return None, "would_seed"
    providers = ["javbus", "fanza", "jav321"]
    if use_javdb:
        providers = ["javbus", "javdb", "fanza", "jav321"]
    last_error: Exception | None = None
    for provider in providers:
        try:
            result = await asyncio.wait_for(
                discover.seed(repo, provider=provider, code=entry.code),
                timeout=seed_timeout,
            )
            work = repo.get_work(result.work_id)
            if work is None:
                raise LookupError(f"seeded work missing: {result.work_id}")
            return work, f"seeded:{provider}"
        except Exception as exc:  # noqa: BLE001 - try next provider
            last_error = exc
            continue
    raise RuntimeError(f"seed failed for {entry.code}: {last_error}")


async def _run(arguments: argparse.Namespace) -> int:
    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    if arguments.proxy:
        updates["proxy_url"] = arguments.proxy
    updates.setdefault("request_timeout_seconds", 12.0)
    updates.setdefault("request_retries", 0)
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    if arguments.list is not None:
        list_path = Path(arguments.list)
    else:
        candidate = settings.data_dir / "javranking" / "list-most-awarded-videos.json"
        list_path = candidate if candidate.is_file() else LIST_PATH
    entries = load_top100(list_path)
    if arguments.limit is not None:
        entries = entries[: max(1, arguments.limit)]

    database = Database(settings.database_url)
    database.initialize()

    proxy = arguments.proxy if arguments.proxy else settings.proxy_url
    client = _http_client(settings, max_connections=settings.provider_concurrency + 4, proxy=proxy)
    provider_clients = tuple(
        _http_client(settings, max_connections=settings.identify_concurrency + 2, proxy=proxy)
        for _ in range(12)
    )
    source = iter(provider_clients)
    # Lean set: reliable without hanging on blocked/geo-gated sources.
    # Include javdb when proxy is configured (NAS).
    provider_list = [
        R18DevProvider(next(source), settings.r18dev_base_url, settings.request_retries),
        FanzaProvider(next(source), settings.fanza_base_url, settings.request_retries),
        JavBusProvider(next(source), settings.javbus_base_url, settings.request_retries),
        Jav321Provider(next(source), settings.jav321_base_url, settings.request_retries),
    ]
    if proxy:
        provider_list.extend(
            [
                JavDBProvider(next(source), settings.javdb_base_url, settings.request_retries),
                JavLibraryProvider(next(source), settings.javlibrary_base_url, settings.request_retries),
                AirAvProvider(next(source), settings.airav_base_url, settings.request_retries),
            ]
        )
    providers = ProviderRegistry(
        provider_list,
        max_concurrent_calls=min(8, settings.provider_concurrency),
    )
    javdb = next(
        (p for p in providers._providers.values() if isinstance(p, JavDBProvider)),
        None,
    )
    fanza = next(
        (p for p in providers._providers.values() if isinstance(p, FanzaProvider)),
        None,
    )
    discover = DiscoverService(providers, javdb, fanza)

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
        enabled=settings.translation_enabled,
        endpoint=settings.translation_endpoint,
        target_language=settings.translation_target_language,
        extra_backends=[b for b in backends if b.name != "google"],
        translate_plot=settings.translation_plot,
    )

    summary: dict[str, Any] = {
        "list": str(list_path),
        "proxy": proxy,
        "dry_run": arguments.dry_run,
        "limit": len(entries),
        "actions": {"seeded": 0, "refreshed": 0, "translated": 0, "artwork": 0, "skipped": 0, "failed": 0},
        "failures": [],
    }

    try:
        with database.session() as session:
            before_map = index_works(Repository(session))
            summary["before"] = completeness_stats(before_map, entries)

        for entry in entries:
            try:
                with database.session() as session:
                    repo = Repository(session)
                    work, seed_status = await ensure_seeded(
                        discover=discover,
                        repo=repo,
                        entry=entry,
                        dry_run=arguments.dry_run,
                        use_javdb=bool(proxy),
                    )
                    if seed_status.startswith("seeded"):
                        summary["actions"]["seeded"] += 1
                    elif seed_status == "would_seed":
                        summary["actions"]["skipped"] += 1
                        print(f"[{entry.position:03d}] {entry.code} · would_seed")
                        continue
                    if work is None:
                        continue

                    if not arguments.dry_run:
                        updated_tags = merge_tags(work.tags or [], SHENZUO_TAGS)
                        repo.update_work_fields(work, tags=updated_tags, lock_edited=False)
                        work = repo.get_work(work.id) or work

                    if arguments.force or needs_refresh(work):
                        if arguments.dry_run:
                            summary["actions"]["refreshed"] += 1
                        else:
                            try:
                                work = await asyncio.wait_for(
                                    refresh_work_metadata(providers=providers, repo=repo, work=work),
                                    timeout=float(arguments.refresh_timeout),
                                )
                            except TimeoutError:
                                print(
                                    f"[{entry.position:03d}] {entry.code} refresh timeout after {arguments.refresh_timeout}s",
                                    flush=True,
                                )
                            summary["actions"]["refreshed"] += 1

                    if not arguments.dry_run and not arguments.no_translate:
                        try:
                            result = await asyncio.wait_for(
                                translator.translate_work(repo, work),
                                timeout=20.0,
                            )
                            if result.status == "translated":
                                summary["actions"]["translated"] += 1
                        except TimeoutError:
                            print(f"[{entry.position:03d}] {entry.code} translate timeout", flush=True)
                        work = repo.get_work(work.id) or work

                    if (
                        not arguments.dry_run
                        and not arguments.no_posters
                        and work.artwork
                    ):
                        art_result, local_paths = await ArtworkStore(
                            settings.data_dir / "artwork",
                            client,
                            max_bytes=settings.artwork_max_bytes,
                        ).acquire(work)
                        if local_paths:
                            repo.update_artwork_local_paths(work, local_paths)
                        summary["actions"]["artwork"] += art_result.downloaded

                    print(
                        f"[{entry.position:03d}] {entry.code} · {seed_status} · "
                        f"title={bool(work.title)} actors={len(work.actors or [])} "
                        f"date={bool(work.release_date)} plot={bool(work.plot)} "
                        f"orig_plot={bool(getattr(work, 'original_plot', None))} "
                        f"runtime={bool(work.runtime_seconds)} rating={work.rating_value is not None}",
                        flush=True,
                    )
            except Exception as exc:  # noqa: BLE001 - continue batch
                summary["actions"]["failed"] += 1
                summary["failures"].append(
                    {"position": entry.position, "code": entry.code, "error": f"{type(exc).__name__}: {exc}"}
                )
                print(f"[{entry.position:03d}] {entry.code} FAIL {type(exc).__name__}: {exc}", file=sys.stderr, flush=True)

        with database.session() as session:
            after_map = index_works(Repository(session))
            summary["after"] = completeness_stats(after_map, entries)
    finally:
        await client.aclose()
        for item in provider_clients:
            await item.aclose()

    print(json.dumps(summary, ensure_ascii=False, indent=2))

    sync_rc = 0
    want_sync = arguments.sync_nas or os.environ.get("SHADOW_MDC_SYNC_NAS", "").strip().lower() in {
        "1",
        "true",
        "yes",
    }
    if want_sync:
        if arguments.dry_run:
            print("skipping --sync-nas because this was a dry-run")
        else:
            sync_rc = _maybe_sync_nas(data_dir=settings.data_dir, dry_run=False)

    if sync_rc != 0:
        return sync_rc
    return 1 if summary["actions"]["failed"] and summary["after"]["cataloged"] == 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None, help="only first N of TOP100")
    parser.add_argument("--list", type=Path, default=None, help="path to list-most-awarded-videos.json")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--proxy", default=None, help="http proxy for provider metadata fetch")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="refresh even if fields look complete")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--no-posters", action="store_true")
    parser.add_argument("--sync-nas", action="store_true")
    parser.add_argument("--refresh-timeout", type=float, default=45.0, help="per-work provider refresh timeout seconds")
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be >= 1")
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
