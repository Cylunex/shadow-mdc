#!/usr/bin/env python3
"""Translate works that still lack a zh title/plot (bounded, resumable).

Usage: python scripts/backfill_translations.py [--limit N] [--checkpoint PATH] [--reset] [--dry-run]
Default checkpoint: <data_dir>/jobs/translation-backfill.json (resumes after last work id).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.models import Work  # noqa: E402
from shadow_mdc.db.repository import Database  # noqa: E402
from shadow_mdc.services.post_ingest import (  # noqa: E402
    build_work_translator,
    translate_pending_works,
    work_needs_translation,
)


async def _run(args: argparse.Namespace) -> int:
    import httpx
    from sqlalchemy import select

    settings = Settings()
    updates = {}
    if args.data_dir:
        updates["data_dir"] = args.data_dir
    if args.database_url:
        updates["database_url"] = args.database_url
    if updates:
        settings = settings.model_copy(update=updates)
    database = Database(settings.database_url)
    checkpoint = args.checkpoint or settings.data_dir / "jobs" / "translation-backfill.json"
    if args.reset:
        checkpoint.unlink(missing_ok=True)
    if args.dry_run:
        with database.session() as session:
            pending = sum(1 for w in session.scalars(select(Work)) if work_needs_translation(w))
        print(json.dumps({"pending": pending, "dry_run": True}))
        return 0
    if not settings.translation_enabled:
        print(json.dumps({"error": "translation disabled"}))
        return 1
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds, follow_redirects=True, proxy=settings.proxy_url
    ) as client:
        stats = await translate_pending_works(
            database,
            build_work_translator(settings, client),
            limit=args.limit,
            checkpoint=checkpoint,
        )
    print(json.dumps(stats.as_dict(), ensure_ascii=False))
    return 0


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=500)
    parser.add_argument("--checkpoint", type=Path, default=None)
    parser.add_argument("--reset", action="store_true")
    parser.add_argument("--dry-run", action="store_true", help="only count pending works")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    raise SystemExit(asyncio.run(_run(parser.parse_args())))


if __name__ == "__main__":
    main()
