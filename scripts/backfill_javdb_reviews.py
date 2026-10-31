#!/usr/bin/env python3
"""Backfill JavDB short reviews into work.reviews (local DB「精选评价」).

Uses the signed app API ``/api/v1/movies/{id}/reviews`` only — does not open or
embed the JavDB website. Existing review rows are merged (no wipe).

Examples:
  .venv/bin/python scripts/backfill_javdb_reviews.py --code SSNI-806 --dry-run
  .venv/bin/python scripts/backfill_javdb_reviews.py --code SSNI-806
  .venv/bin/python scripts/backfill_javdb_reviews.py --limit 50
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

import httpx

from shadow_mdc.config import Settings
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.providers.javdb_api import build_javdb_app_api


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--code", action="append", default=[], help="Primary code to refresh (repeatable)")
    parser.add_argument("--limit", type=int, default=0, help="When no --code, process up to N works missing javdb reviews")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--reviews-limit", type=int, default=20)
    return parser.parse_args()


def _javdb_id(repo: Repository, work_id: str) -> str | None:
    for identity in repo.identities_for_work(work_id):
        if getattr(identity, "provider", None) == "javdb" and getattr(identity, "value", None):
            return str(identity.value)
    return None


def _has_javdb_review(work) -> bool:
    for item in work.reviews or []:
        if isinstance(item, dict) and str(item.get("provider", "")).strip() == "javdb":
            if str(item.get("text", "")).strip():
                return True
    return False


async def _run(args: argparse.Namespace) -> int:
    settings = Settings()
    database = Database(settings.database_url)
    database.initialize()
    async with httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        proxy=settings.proxy_url,
    ) as client:
        api = build_javdb_app_api(settings, client)
        if api is None:
            print("javdb app API disabled or hosts empty", file=sys.stderr)
            return 2
        updated = 0
        skipped = 0
        failed = 0
        with database.session() as session:
            repo = Repository(session)
            targets: list[tuple[str, str, str | None]] = []  # work_id, movie_id, code
            if args.code:
                for code in args.code:
                    work = repo.find_work_by_code(code.strip().upper())
                    if work is None:
                        print(f"missing work for code {code}", file=sys.stderr)
                        failed += 1
                        continue
                    movie_id = _javdb_id(repo, work.id)
                    if not movie_id:
                        print(f"{code}: no javdb identity", file=sys.stderr)
                        failed += 1
                        continue
                    targets.append((work.id, movie_id, work.primary_code))
            else:
                # Works that already have a javdb identity but no javdb review text yet.
                for work in repo.list_works():
                    if args.limit and len(targets) >= args.limit:
                        break
                    if _has_javdb_review(work):
                        continue
                    movie_id = _javdb_id(repo, work.id)
                    if not movie_id:
                        continue
                    targets.append((work.id, movie_id, work.primary_code))

            for work_id, movie_id, code in targets:
                label = code or work_id
                try:
                    highlights = await api.reviews(movie_id, limit=args.reviews_limit)
                except Exception as exc:  # noqa: BLE001
                    print(f"{label}: reviews failed: {type(exc).__name__}: {exc}", file=sys.stderr)
                    failed += 1
                    continue
                if not highlights:
                    print(f"{label}: no reviews")
                    skipped += 1
                    continue
                work = repo.get_work(work_id)
                if work is None:
                    failed += 1
                    continue
                # Minimal ProviderRecord so upsert merges reviews only (+ identity).
                record = ProviderRecord(
                    provider="javdb",
                    external_id=movie_id,
                    title=work.title or label or movie_id,
                    code=work.primary_code,
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    reviews=highlights,
                )
                if args.dry_run:
                    print(f"{label}: would merge {len(highlights)} reviews")
                    updated += 1
                    continue
                repo.merge_provider_into_work(work, record, overwrite=False)
                print(f"{label}: merged {len(highlights)} reviews")
                updated += 1
        print(f"done updated={updated} skipped={skipped} failed={failed} dry_run={args.dry_run}")
        return 0 if failed == 0 else 1


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
