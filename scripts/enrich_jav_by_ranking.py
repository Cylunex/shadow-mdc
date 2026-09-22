#!/usr/bin/env python3
"""Enrich JAV works missing fields, ordered by ranking priority.

Priority (default):
  1. 神作 TOP100 (list-most-awarded-videos.json)
  2. Yearly TOP250 recent→older (list-javdb-top250-YYYY.json)
  3. Codes appearing in recent daily-chart-runs/
  4. Remaining cataloged JAV with gaps (optional --include-catalog-gaps)

Reuses provider refresh / translation / artwork patterns from enrich_shenzuo_top100.
Prefer website sample images (jav321/FANZA); does not invent metadata.

Example::

    PYTHONPATH=src .venv/bin/python scripts/enrich_jav_by_ranking.py --dry-run --limit 30
    PYTHONPATH=src .venv/bin/python scripts/enrich_jav_by_ranking.py --phases shenzuo,yearly --years 2025,2024 --limit 50
    PYTHONPATH=src .venv/bin/python scripts/enrich_jav_by_ranking.py --proxy http://192.168.0.110:7893 --sync-nas
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
from typing import Any, Iterable

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.models import Work  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.domain import IdentityHints  # noqa: E402
from shadow_mdc.enums import ContentFamily, MatchDecision, MediaCategory, QueryMode  # noqa: E402
from shadow_mdc.identity import extract_code  # noqa: E402
from shadow_mdc.matching import rank_candidates  # noqa: E402
from shadow_mdc.media.artwork import ArtworkStore  # noqa: E402
from shadow_mdc.normalize_code import normalize_code, to_comparison_key  # noqa: E402
from shadow_mdc.providers.airav import AirAvProvider  # noqa: E402
from shadow_mdc.providers.base import ProviderRegistry  # noqa: E402
from shadow_mdc.providers.fanza import FanzaProvider  # noqa: E402
from shadow_mdc.providers.jav321 import Jav321Provider  # noqa: E402
from shadow_mdc.providers.javbus import JavBusProvider  # noqa: E402
from shadow_mdc.providers.javdb import JavDBProvider  # noqa: E402
from shadow_mdc.providers.javlibrary import JavLibraryProvider  # noqa: E402
from shadow_mdc.providers.r18dev import R18DevProvider  # noqa: E402
from shadow_mdc.services.daily_chart_seed import merge_tags  # noqa: E402
from shadow_mdc.services.discover import DiscoverService  # noqa: E402
from shadow_mdc.services.javranking_client import BROWSER_UA  # noqa: E402
from shadow_mdc.services.translation import (  # noqa: E402
    GoogleTitleTranslator,
    TranslationCache,
    build_translation_backends,
)
from shadow_mdc.services.work_samples import count_samples, enrich_work_samples  # noqa: E402

TRACK_FIELDS = (
    "title",
    "original_title",
    "actors",
    "release_date",
    "runtime_seconds",
    "studio",
    "tags",
    "plot",
    "original_plot",
    "artwork",
    "rating_value",
    "samples",
)


@dataclass(frozen=True, slots=True)
class RankEntry:
    phase: str
    position: int
    code: str
    title: str
    source: str


def _http_client(settings: Settings, *, max_connections: int, proxy: str | None) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept-Language": "ja,zh-CN;q=0.9,en-US;q=0.8,en;q=0.7",
        },
        proxy=proxy if proxy is not None else settings.proxy_url,
        limits=httpx.Limits(max_connections=max_connections, max_keepalive_connections=0),
    )


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
    if name == "studio":
        return bool(work.studio)
    if name == "tags":
        return bool(work.tags)
    if name == "plot":
        return bool((work.plot or "").strip())
    if name == "original_plot":
        return bool((getattr(work, "original_plot", None) or "").strip())
    if name == "artwork":
        art = work.artwork if isinstance(work.artwork, list) else []
        return len(art) > 0
    if name == "rating_value":
        return work.rating_value is not None
    if name == "samples":
        return count_samples(work) >= 3
    return False


def needs_refresh(work: Work) -> bool:
    required = ("actors", "studio", "release_date", "plot", "artwork", "samples")
    return any(not field_present(work, name) for name in required)


def load_list_entries(path: Path, *, phase: str) -> list[RankEntry]:
    if not path.is_file():
        return []
    payload = json.loads(path.read_text(encoding="utf-8"))
    videos = payload.get("videos") or payload.get("items") or []
    out: list[RankEntry] = []
    for item in videos:
        raw = (item.get("code") or "").strip()
        code = normalize_code(raw) or raw
        if not code:
            continue
        out.append(
            RankEntry(
                phase=phase,
                position=int(item.get("position") or item.get("rank") or len(out) + 1),
                code=code,
                title=str(item.get("title") or code),
                source=str(path.name),
            )
        )
    return out


def load_daily_chart_entries(runs_dir: Path, *, max_runs: int = 5) -> list[RankEntry]:
    if not runs_dir.is_dir():
        return []
    files = sorted(runs_dir.glob("*.json"), reverse=True)[:max_runs]
    seen: set[str] = set()
    out: list[RankEntry] = []
    for path in files:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        seeded = payload.get("seeded") or payload.get("works") or payload.get("candidates") or []
        for idx, item in enumerate(seeded, start=1):
            if not isinstance(item, dict):
                continue
            raw = (item.get("code") or item.get("primary_code") or "").strip()
            code = normalize_code(raw) or raw
            if not code:
                continue
            key = to_comparison_key(code)
            if key in seen:
                continue
            seen.add(key)
            out.append(
                RankEntry(
                    phase="daily",
                    position=idx,
                    code=code,
                    title=str(item.get("title") or code),
                    source=path.name,
                )
            )
    return out


def build_priority_queue(
    data_dir: Path,
    *,
    phases: Iterable[str],
    years: list[int],
    include_catalog_gaps: bool,
    repo: Repository | None,
) -> list[RankEntry]:
    queue: list[RankEntry] = []
    seen: set[str] = set()

    def add_all(entries: list[RankEntry]) -> None:
        for entry in entries:
            key = to_comparison_key(entry.code)
            if not key or key in seen:
                continue
            seen.add(key)
            queue.append(entry)

    phase_set = {p.strip() for p in phases}
    ranking_dir = data_dir / "javranking"

    if "shenzuo" in phase_set:
        add_all(load_list_entries(ranking_dir / "list-most-awarded-videos.json", phase="shenzuo"))

    if "yearly" in phase_set:
        for year in years:
            add_all(
                load_list_entries(
                    ranking_dir / f"list-javdb-top250-{year}.json",
                    phase=f"yearly-{year}",
                )
            )

    if "daily" in phase_set:
        add_all(load_daily_chart_entries(data_dir / "daily-chart-runs"))

    if include_catalog_gaps and repo is not None:
        position = 0
        for work in repo.list_works():
            if work.family != ContentFamily.JAV.value:
                continue
            if not work.primary_code:
                continue
            if not needs_refresh(work):
                continue
            code = normalize_code(work.primary_code) or work.primary_code
            key = to_comparison_key(code)
            if key in seen:
                continue
            seen.add(key)
            position += 1
            queue.append(
                RankEntry(
                    phase="catalog-gap",
                    position=position,
                    code=code,
                    title=work.title or code,
                    source="db",
                )
            )
    return queue


async def refresh_work_metadata(
    *,
    providers: ProviderRegistry,
    repo: Repository,
    work: Work,
) -> Work:
    code_raw = work.primary_code or ""
    code, family = extract_code(
        code_raw, MediaCategory(work.category) if work.category else MediaCategory.JAPAN
    )
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
    entry: RankEntry,
    dry_run: bool,
    use_javdb: bool,
    seed_timeout: float,
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
        except Exception as exc:  # noqa: BLE001
            last_error = exc
            continue
    raise RuntimeError(f"seed failed for {entry.code}: {last_error}")


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


def gap_list(work: Work) -> list[str]:
    return [name for name in TRACK_FIELDS if not field_present(work, name)]


async def _run(arguments: argparse.Namespace) -> int:
    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    if arguments.proxy:
        updates["proxy_url"] = arguments.proxy
    updates.setdefault("request_timeout_seconds", 15.0)
    updates.setdefault("request_retries", 0)
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    years = [int(y.strip()) for y in arguments.years.split(",") if y.strip()]
    phases = [p.strip() for p in arguments.phases.split(",") if p.strip()]

    database = Database(settings.database_url)
    database.initialize()

    with database.session() as session:
        queue = build_priority_queue(
            settings.data_dir,
            phases=phases,
            years=years,
            include_catalog_gaps=arguments.include_catalog_gaps,
            repo=Repository(session),
        )
    if arguments.limit is not None:
        queue = queue[: max(1, arguments.limit)]

    proxy = arguments.proxy if arguments.proxy else settings.proxy_url
    client = _http_client(settings, max_connections=settings.provider_concurrency + 4, proxy=proxy)
    provider_clients = tuple(
        _http_client(settings, max_connections=settings.identify_concurrency + 2, proxy=proxy)
        for _ in range(12)
    )
    source = iter(provider_clients)
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
    javdb = next((p for p in providers._providers.values() if isinstance(p, JavDBProvider)), None)
    fanza = next((p for p in providers._providers.values() if isinstance(p, FanzaProvider)), None)
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
        enabled=settings.translation_enabled and not arguments.no_translate,
        endpoint=settings.translation_endpoint,
        target_language=settings.translation_target_language,
        extra_backends=[b for b in backends if b.name != "google"],
        translate_plot=settings.translation_plot,
    )

    summary: dict[str, Any] = {
        "phases": phases,
        "years": years,
        "proxy": proxy,
        "dry_run": arguments.dry_run,
        "queued": len(queue),
        "actions": {
            "seeded": 0,
            "refreshed": 0,
            "translated": 0,
            "artwork": 0,
            "samples": 0,
            "skipped_complete": 0,
            "failed": 0,
        },
        "filled": {name: 0 for name in TRACK_FIELDS},
        "failures": [],
        "by_phase": {},
    }

    try:
        for entry in queue:
            phase_stats = summary["by_phase"].setdefault(
                entry.phase, {"seen": 0, "refreshed": 0, "seeded": 0, "failed": 0}
            )
            phase_stats["seen"] += 1
            try:
                with database.session() as session:
                    repo = Repository(session)
                    work, seed_status = await ensure_seeded(
                        discover=discover,
                        repo=repo,
                        entry=entry,
                        dry_run=arguments.dry_run,
                        use_javdb=bool(proxy),
                        seed_timeout=float(arguments.seed_timeout),
                    )
                    if seed_status.startswith("seeded"):
                        summary["actions"]["seeded"] += 1
                        phase_stats["seeded"] += 1
                    if work is None:
                        print(f"[{entry.phase} #{entry.position}] {entry.code} · {seed_status}")
                        continue

                    before_gaps = gap_list(work)
                    if not arguments.force and not before_gaps:
                        summary["actions"]["skipped_complete"] += 1
                        print(f"[{entry.phase} #{entry.position}] {entry.code} · complete")
                        continue
                    # Ratings are sparsely available from current providers; do not
                    # burn the batch budget on rating-only rows unless forced.
                    meaningful = [g for g in before_gaps if g != "rating_value"]
                    if not arguments.force and not meaningful:
                        summary["actions"]["skipped_complete"] += 1
                        print(f"[{entry.phase} #{entry.position}] {entry.code} · skip rating-only")
                        continue

                    if arguments.dry_run:
                        summary["actions"]["refreshed"] += 1
                        print(
                            f"[dry {entry.phase} #{entry.position}] {entry.code} gaps={before_gaps}"
                        )
                        continue

                    rank_tags = (entry.phase, "ranking-enrich")
                    if entry.phase == "shenzuo":
                        rank_tags = ("javranking", "most-awarded-videos", "shenzuo")
                    repo.update_work_fields(
                        work, tags=merge_tags(work.tags or [], rank_tags), lock_edited=False
                    )
                    work = repo.get_work(work.id) or work

                    if arguments.force or needs_refresh(work) or "rating_value" in before_gaps:
                        try:
                            work = await asyncio.wait_for(
                                refresh_work_metadata(providers=providers, repo=repo, work=work),
                                timeout=float(arguments.refresh_timeout),
                            )
                        except TimeoutError:
                            print(
                                f"[{entry.phase} #{entry.position}] {entry.code} refresh timeout",
                                flush=True,
                            )
                        summary["actions"]["refreshed"] += 1
                        phase_stats["refreshed"] += 1

                    if not arguments.no_translate:
                        try:
                            result = await asyncio.wait_for(
                                translator.translate_work(repo, work), timeout=20.0
                            )
                            if result.status == "translated":
                                summary["actions"]["translated"] += 1
                        except TimeoutError:
                            print(f"[{entry.phase} #{entry.position}] {entry.code} translate timeout")
                        work = repo.get_work(work.id) or work

                    if not arguments.no_posters and work.artwork:
                        art_result, local_paths = await ArtworkStore(
                            settings.data_dir / "artwork",
                            client,
                            max_bytes=settings.artwork_max_bytes,
                        ).acquire(work)
                        if local_paths:
                            repo.update_artwork_local_paths(work, local_paths)
                        summary["actions"]["artwork"] += art_result.downloaded
                        work = repo.get_work(work.id) or work

                    if not arguments.no_samples and count_samples(work) < 3:
                        sample_result = await enrich_work_samples(
                            repo,
                            work,
                            artwork_root=settings.data_dir / "artwork",
                            http_client=client,
                            max_bytes=settings.artwork_max_bytes,
                            target_count=5,
                        )
                        summary["actions"]["samples"] += sample_result.web_downloaded + sample_result.local_generated
                        work = repo.get_work(work.id) or work

                    after_gaps = gap_list(work)
                    for name in TRACK_FIELDS:
                        if name in before_gaps and name not in after_gaps:
                            summary["filled"][name] += 1
                    print(
                        f"[{entry.phase} #{entry.position}] {entry.code} · {seed_status} · "
                        f"closed={sorted(set(before_gaps) - set(after_gaps))} remain={after_gaps}",
                        flush=True,
                    )
                    await asyncio.sleep(float(arguments.delay))
            except Exception as exc:  # noqa: BLE001
                summary["actions"]["failed"] += 1
                phase_stats["failed"] += 1
                summary["failures"].append(
                    {
                        "phase": entry.phase,
                        "position": entry.position,
                        "code": entry.code,
                        "error": f"{type(exc).__name__}: {exc}",
                    }
                )
                print(
                    f"[{entry.phase} #{entry.position}] {entry.code} FAIL {type(exc).__name__}: {exc}",
                    file=sys.stderr,
                    flush=True,
                )
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
    if want_sync and not arguments.dry_run:
        sync_rc = _maybe_sync_nas(data_dir=settings.data_dir, dry_run=False)
    elif want_sync and arguments.dry_run:
        print("skipping --sync-nas because this was a dry-run")
    return sync_rc if sync_rc else (1 if summary["actions"]["failed"] and summary["actions"]["refreshed"] == 0 else 0)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--proxy", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--no-translate", action="store_true")
    parser.add_argument("--no-posters", action="store_true")
    parser.add_argument("--no-samples", action="store_true")
    parser.add_argument("--sync-nas", action="store_true")
    parser.add_argument(
        "--phases",
        default="shenzuo,yearly,daily",
        help="comma list: shenzuo,yearly,daily",
    )
    parser.add_argument(
        "--years",
        default="2025,2024,2023,2022,2021",
        help="yearly TOP250 years, recent first",
    )
    parser.add_argument("--include-catalog-gaps", action="store_true")
    parser.add_argument("--delay", type=float, default=0.4)
    parser.add_argument("--refresh-timeout", type=float, default=45.0)
    parser.add_argument("--seed-timeout", type=float, default=25.0)
    arguments = parser.parse_args()
    if arguments.limit is not None and arguments.limit < 1:
        raise SystemExit("--limit must be >= 1")
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
