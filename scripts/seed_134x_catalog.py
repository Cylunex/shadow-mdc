#!/usr/bin/env python3
"""Discover JAV codes (+ optional X handles) from 134x.com long-clip listings.

Metadata / catalog only: writes ``data/134x-runs/<date>.json`` and seeds missing
codes via DiscoverService. Does not download or store stream URLs.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import date
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.enums import MediaCategory
from shadow_mdc.media.artwork import ArtworkStore
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.providers.fanza import FanzaProvider
from shadow_mdc.providers.javdb import JavDBProvider
from shadow_mdc.providers.javdb_api import build_javdb_app_api
from shadow_mdc.services.daily_chart_seed import merge_tags
from shadow_mdc.services.discover import DiscoverService
from shadow_mdc.services.non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    NonJavActorProfile,
)
from shadow_mdc.services.x134_catalog import (
    BROWSER_UA,
    DEFAULT_BASE_URL,
    LIST_PATHS,
    build_catalog_snapshot,
)

_TAG = "134x-catalog"


async def _run(arguments: argparse.Namespace) -> int:
    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
        proxy=settings.proxy_url,
    )
    try:
        snapshot = await build_catalog_snapshot(
            client,
            base_url=arguments.base_url,
            list_paths=tuple(arguments.lists.split(",")) if arguments.lists else LIST_PATHS,
            detail_limit=arguments.detail_limit,
        )
    finally:
        if arguments.catalog_only:
            await client.aclose()

    run_day = date.today().isoformat()
    runs_dir = settings.data_dir / "134x-runs"
    runs_dir.mkdir(parents=True, exist_ok=True)
    run_path = runs_dir / f"{run_day}.json"
    payload = snapshot.model_dump(mode="json")
    run_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"134x catalog: lists={list(snapshot.lists)} refs={len(snapshot.refs)} "
        f"details={len(snapshot.details)} codes={len(snapshot.codes)} "
        f"handles={len(snapshot.handles)} log={run_path}"
    )

    if arguments.import_handles and snapshot.handles:
        store = NonJavActorCatalogStore(settings.data_dir / "non-jav-actors.json")
        added = 0
        for handle in snapshot.handles:
            name = f"@{handle}"
            existing = store.get(name) or store.get(handle)
            if existing is not None:
                continue
            profile = NonJavActorProfile(
                name=name,
                aliases=(handle,),
                groups=("twitter", "blogger", "134x"),
                categories=(MediaCategory.OTHER,),
                x_handle=handle,
                notes="discovered from 134x.com long-clip catalog (unverified handle)",
            )
            store.upsert(profile)
            added += 1
        print(f"134x handles imported as non-JAV actors: added={added}")

    if arguments.catalog_only:
        return 0

    database = Database(settings.database_url)
    database.initialize()
    javdb = JavDBProvider(client, settings.javdb_base_url, settings.request_retries)
    fanza = FanzaProvider(client, settings.fanza_base_url, settings.request_retries)
    providers = ProviderRegistry([fanza, javdb], max_concurrent_calls=4)
    discover = DiscoverService(
        providers,
        javdb,
        fanza,
        r18_dump_path=settings.resolved_r18_dump_db(),
        javdb_api=build_javdb_app_api(settings, client),
    )

    codes = list(snapshot.codes)[: arguments.seed_limit]
    results: list[dict[str, object]] = []
    try:
        for code in codes:
            entry: dict[str, object] = {"code": code}
            try:
                with database.session() as session:
                    repo = Repository(session)
                    seed = await discover.seed(repo, provider="fanza", code=code)
                    work = repo.get_work(seed.work_id)
                    if work is None:
                        raise LookupError(f"seeded work missing: {seed.work_id}")
                    repo.update_work_fields(
                        work, tags=merge_tags(work.tags or [], _TAG), lock_edited=False
                    )
                    art = 0
                    if not arguments.no_posters and work.artwork:
                        art_result, local_paths = await ArtworkStore(
                            settings.data_dir / "artwork",
                            client,
                            max_bytes=settings.artwork_max_bytes,
                        ).acquire(work)
                        if local_paths:
                            repo.update_artwork_local_paths(work, local_paths)
                        art = art_result.downloaded
                    entry.update(
                        ok=True,
                        work_id=work.id,
                        created=seed.created,
                        fallback=seed.fallback,
                        title=work.title,
                        artwork_downloaded=art,
                    )
            except Exception as exc:
                entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            results.append(entry)
            status = "ok" if entry.get("ok") else entry.get("error")
            print(f"  seed {code}: {status}")
    finally:
        discover.close()
        await client.aclose()

    ok = sum(1 for r in results if r.get("ok"))
    print(f"134x seed done: attempted={len(results)} ok={ok}")
    if arguments.json:
        print(json.dumps({"snapshot_codes": codes, "results": results}, ensure_ascii=False, indent=2))
    return 0 if ok or not codes else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--base-url", default=DEFAULT_BASE_URL)
    parser.add_argument(
        "--lists",
        default=",".join(LIST_PATHS),
        help="comma-separated list paths (default: /popular,/featured,/)",
    )
    parser.add_argument("--detail-limit", type=int, default=25)
    parser.add_argument("--seed-limit", type=int, default=20, help="max codes to seed")
    parser.add_argument("--catalog-only", action="store_true", help="write catalog JSON only")
    parser.add_argument(
        "--import-handles",
        action="store_true",
        help="upsert discovered @handles into non-jav-actors.json (unverified)",
    )
    parser.add_argument("--no-posters", action="store_true")
    parser.add_argument("--json", action="store_true")
    arguments = parser.parse_args()
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
