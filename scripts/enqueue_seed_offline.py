#!/usr/bin/env python3
"""Enqueue 115/OpenList offline for newly seeded daily chart/hot works.

Typical NAS catch-up after catalog sync::

    ssh nas 'cd /data/project/shadow-mdc/current && \\
      SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\
      SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\
      PYTHONPATH=src .venv/bin/python scripts/enqueue_seed_offline.py --today'

Or pass codes / work ids explicitly.
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
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.providers.fanza import FanzaProvider
from shadow_mdc.providers.javdb import JavDBProvider
from shadow_mdc.providers.javdb_api import build_javdb_app_api
from shadow_mdc.providers.sukebei import SukebeiClient
from shadow_mdc.services.discover import DiscoverService
from shadow_mdc.services.pan import PanService
from shadow_mdc.services.seed_auto_offline import (
    enqueue_seeded_offline,
    resolve_work_ids_by_codes,
    resolve_work_ids_for_day,
    work_ids_from_run_logs,
)

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
    pan = PanService(data_dir=settings.data_dir, proxy_url=settings.proxy_url)

    day = date.fromisoformat(arguments.day) if arguments.day else date.today()
    work_ids: list[str] = []
    seen: set[str] = set()

    def _add(ids: list[str]) -> None:
        for work_id in ids:
            if work_id not in seen:
                seen.add(work_id)
                work_ids.append(work_id)

    if arguments.work_id:
        _add(list(arguments.work_id))
    if arguments.code:
        with database.session() as session:
            _add(resolve_work_ids_by_codes(Repository(session), arguments.code))
    if arguments.from_run_logs or arguments.today:
        _add(work_ids_from_run_logs(settings.data_dir, day=day, created_only=not arguments.include_existing))
    if arguments.from_day_tags or arguments.today:
        with database.session() as session:
            _add(
                resolve_work_ids_for_day(
                    Repository(session),
                    day=day,
                    include_chart=not arguments.hot_only,
                    include_hot=not arguments.chart_only,
                )
            )

    if not work_ids:
        print(json.dumps({"day": day.isoformat(), "work_ids": [], "note": "nothing to enqueue"}))
        return 0

    client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": _BROWSER_UA},
        proxy=settings.proxy_url,
        limits=httpx.Limits(max_connections=settings.provider_concurrency + 2, max_keepalive_connections=0),
    )
    javdb = JavDBProvider(client, settings.javdb_base_url, settings.request_retries)
    fanza = FanzaProvider(client, settings.fanza_base_url, settings.request_retries)
    providers = ProviderRegistry([javdb, fanza], max_concurrent_calls=settings.provider_concurrency)
    discover = DiscoverService(
        providers,
        javdb,
        fanza,
        r18_dump_path=settings.resolved_r18_dump_db(),
        javdb_api=build_javdb_app_api(settings, client),
        sukebei=SukebeiClient(client, retries=settings.request_retries),
    )
    try:
        if arguments.dry_run:
            print(json.dumps({"day": day.isoformat(), "work_ids": work_ids, "dry_run": True}, ensure_ascii=False, indent=2))
            return 0
        stats = await enqueue_seeded_offline(
            database=database,
            pan=pan,
            discover=discover,
            work_ids=work_ids,
            pace_seconds=arguments.pace,
        )
    finally:
        discover.close()
        await client.aclose()

    payload = {"day": day.isoformat(), "work_ids": work_ids, **stats.as_dict()}
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 1 if stats.errors and stats.submitted == 0 and stats.reused == 0 else 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--day", default=None, help="YYYY-MM-DD (default today)")
    parser.add_argument("--today", action="store_true", help="from today's run logs + day tags")
    parser.add_argument("--from-run-logs", action="store_true")
    parser.add_argument("--from-day-tags", action="store_true")
    parser.add_argument("--chart-only", action="store_true")
    parser.add_argument("--hot-only", action="store_true")
    parser.add_argument("--include-existing", action="store_true", help="also re-check non-created run-log rows")
    parser.add_argument("--work-id", action="append", default=[])
    parser.add_argument("--code", action="append", default=[])
    parser.add_argument("--pace", type=float, default=2.0)
    parser.add_argument("--dry-run", action="store_true")
    arguments = parser.parse_args()
    if not (
        arguments.today
        or arguments.from_run_logs
        or arguments.from_day_tags
        or arguments.work_id
        or arguments.code
    ):
        raise SystemExit("pass --today and/or --work-id/--code/--from-run-logs/--from-day-tags")
    raise SystemExit(asyncio.run(_run(arguments)))


if __name__ == "__main__":
    main()
