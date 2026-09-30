#!/usr/bin/env python3
"""Seed specific 番号 into the catalog (online first, offline r18 dump fallback).

Useful to retry intake failures from ``seed_daily_hot.py`` / ``seed_daily_chart.py``
(``LookupError: no provider records``). Online providers (FANZA, JavDB) are tried
first; when they return nothing and ``r18_dump.db`` exists (NAS:
``$SHADOW_MDC_DATA_DIR/r18-dumps/r18_dump.db``) the work is created from the dump.
Existing works are never overwritten.

Example (NAS)::

    ssh nas 'cd /data/project/shadow-mdc/current && \\
      SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\
      SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\
      PYTHONPATH=src .venv/bin/python scripts/seed_codes.py START-634 ABF-387 --tag daily-hot'
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.media.artwork import ArtworkStore
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.providers.fanza import FanzaProvider
from shadow_mdc.providers.javdb import JavDBProvider
from shadow_mdc.services.daily_hot_seed import merge_tags
from shadow_mdc.services.discover import DiscoverService

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)


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
    database = Database(settings.database_url)
    database.initialize()

    client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA, "Accept-Language": "ja,zh-CN;q=0.9,en;q=0.7"},
        proxy=settings.proxy_url,
    )
    javdb = JavDBProvider(client, settings.javdb_base_url, settings.request_retries)
    fanza = FanzaProvider(client, settings.fanza_base_url, settings.request_retries)
    providers = ProviderRegistry([fanza, javdb], max_concurrent_calls=4)
    dump_path = settings.resolved_r18_dump_db()
    discover = DiscoverService(providers, javdb, fanza, r18_dump_path=dump_path)
    if not discover.r18_fallback_available:
        print(f"note: r18 dump not found at {dump_path}; offline fallback disabled", file=sys.stderr)

    results: list[dict[str, object]] = []
    try:
        for code in arguments.codes:
            entry: dict[str, object] = {"code": code}
            try:
                with database.session() as session:
                    repo = Repository(session)
                    seed = await discover.seed(repo, provider="fanza", code=code)
                    work = repo.get_work(seed.work_id)
                    if work is None:
                        raise LookupError(f"seeded work missing: {seed.work_id}")
                    if arguments.tag:
                        repo.update_work_fields(
                            work, tags=merge_tags(work.tags or [], arguments.tag), lock_edited=False
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
                        actors=list(work.actors or []),
                        studio=work.studio,
                        release_date=work.release_date.isoformat() if work.release_date else None,
                        runtime_seconds=work.runtime_seconds,
                        artwork_downloaded=art,
                    )
            except Exception as exc:  # noqa: BLE001 - report per code
                entry.update(ok=False, error=f"{type(exc).__name__}: {exc}")
            results.append(entry)
    finally:
        discover.close()
        await client.aclose()

    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if all(item.get("ok") for item in results) else 1


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("codes", nargs="+", help="番号 such as START-634")
    parser.add_argument("--tag", action="append", default=[], help="extra tag(s) to merge onto the work")
    parser.add_argument("--no-posters", action="store_true", help="skip artwork download")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
