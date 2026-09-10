#!/usr/bin/env python3
"""Seed today's top chart works (+ actors) from multi-list discover rankings.

Preferred: run **on NAS** against shared data (no export/import hop)::

    ssh nas 'cd /data/project/shadow-mdc/current && \\
      SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\
      SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\
      PYTHONPATH=src .venv/bin/python scripts/seed_daily_chart.py --limit 10'

Local alternative: run here, then ``scripts/sync_catalog_to_nas.sh`` for an
incremental catalog push. Magnets are left alone (display/save only if the app
already supports them); this script never talks to 115 or downloaders.
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
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.providers.javdb import JavDBProvider
from shadow_mdc.services.daily_chart_seed import DEFAULT_LISTS, seed_daily_chart
from shadow_mdc.services.discover import DiscoverList, DiscoverService


def _http_client(settings: Settings, *, max_connections: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
        proxy=settings.proxy_url,
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=0,
        ),
    )


def _parse_lists(raw: str | None) -> tuple[DiscoverList, ...]:
    if not raw:
        return DEFAULT_LISTS
    allowed = set(DEFAULT_LISTS)
    lists: list[DiscoverList] = []
    for part in raw.split(","):
        name = part.strip()
        if not name:
            continue
        if name not in allowed:
            raise SystemExit(f"unknown list: {name}; choose from {sorted(allowed)}")
        lists.append(name)  # type: ignore[arg-type]
    if not lists:
        raise SystemExit("at least one --lists value is required")
    return tuple(lists)


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

    client = _http_client(settings, max_connections=settings.provider_concurrency + 2)
    javdb = JavDBProvider(client, settings.javdb_base_url, settings.request_retries)
    providers = ProviderRegistry([javdb], max_concurrent_calls=settings.provider_concurrency)
    discover = DiscoverService(providers, javdb)

    try:
        with database.session() as session:
            result = await seed_daily_chart(
                discover=discover,
                repo=Repository(session),
                data_dir=settings.data_dir,
                http_client=client,
                limit=arguments.limit,
                dry_run=arguments.dry_run,
                lists=_parse_lists(arguments.lists),
                download_posters=not arguments.no_posters,
                artwork_max_bytes=settings.artwork_max_bytes,
                persist_log=not arguments.no_log,
            )
    finally:
        await client.aclose()

    summary = {
        "run_date": result.run_date,
        "dry_run": result.dry_run,
        "lists_scanned": list(result.lists_scanned),
        "considered": len(result.considered),
        "seeded": [
            {
                "rank": item.rank,
                "code": item.code,
                "title": item.title,
                "actors": list(item.actors),
                "score": item.score,
                "created": item.created,
                "tags": list(item.tags),
            }
            for item in result.seeded
        ],
        "skipped": len(result.skipped),
        "failures": [{"key": f.key, "error": f.error} for f in result.failures],
        "run_log_path": result.run_log_path,
    }

    if arguments.json:
        print(result.model_dump_json(indent=2))
    else:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        print(
            f"\ndaily-chart {result.run_date}: "
            f"considered={len(result.considered)} seeded={len(result.seeded)} "
            f"skipped={len(result.skipped)} failures={len(result.failures)}"
            + (" [dry-run]" if result.dry_run else "")
        )
    return 1 if result.failures and not result.seeded else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=10, help="max works to seed (default 10)")
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="data directory (env SHADOW_MDC_DATA_DIR / Settings.data_dir)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL (env SHADOW_MDC_DATABASE_URL)",
    )
    parser.add_argument(
        "--lists",
        default=None,
        help="comma-separated discover lists (default: rankings_daily,weekly,monthly,latest)",
    )
    parser.add_argument("--dry-run", action="store_true", help="score and select only; no DB writes")
    parser.add_argument("--no-posters", action="store_true", help="skip artwork download")
    parser.add_argument("--no-log", action="store_true", help="do not write data/daily-chart-runs/*.json")
    parser.add_argument("--json", action="store_true", help="print full structured result JSON")
    arguments = parser.parse_args()
    if arguments.limit < 1:
        raise SystemExit("--limit must be >= 1")
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
