#!/usr/bin/env python3
"""Fill empty JAV actor portraits from gfriends/gfriends Filetree.

Examples::

    PYTHONPATH=src .venv/bin/python scripts/fill_gfriends_actor_images.py --dry-run --limit 50
    PYTHONPATH=src .venv/bin/python scripts/fill_gfriends_actor_images.py --limit 200
    PYTHONPATH=src .venv/bin/python scripts/fill_gfriends_actor_images.py --cdn-only --force-refresh
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.repository import Database  # noqa: E402
from shadow_mdc.services.gfriends import GfriendsActorImageResolver  # noqa: E402
from shadow_mdc.services.gfriends_fill import (  # noqa: E402
    fill_actor_images_from_gfriends,
    localize_cdn_actor_images,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--cdn-only", action="store_true", help="Store CDN URL only; do not download")
    parser.add_argument("--force-refresh", action="store_true", help="Force re-download Filetree.json")
    parser.add_argument(
        "--localize",
        action="store_true",
        help="Download existing CDN image_url values into local actor-images",
    )
    args = parser.parse_args()

    settings = Settings()
    settings.ensure_directories()
    cache_path = settings.data_dir / "cache" / "gfriends" / "Filetree.json"
    resolver = GfriendsActorImageResolver(
        filetree_url=settings.gfriends_filetree_url,
        cdn_base_url=settings.gfriends_cdn_base_url,
        cache_path=cache_path,
        cache_ttl_hours=settings.gfriends_cache_ttl_hours,
    )
    database = Database(settings.database_url)
    database.initialize()
    http_client = None
    if settings.proxy_url:
        http_client = __import__("httpx").Client(
            timeout=45.0,
            proxy=settings.proxy_url,
            headers={"User-Agent": settings.user_agent},
            follow_redirects=True,
        )
    try:
        with database.session() as session:
            from shadow_mdc.db.repository import Repository

            repo = Repository(session)
            if args.localize:
                stats = localize_cdn_actor_images(
                    repo,
                    actor_images_dir=settings.data_dir / "actor-images",
                    limit=args.limit,
                    dry_run=args.dry_run,
                    http_client=http_client,
                )
            else:
                stats = fill_actor_images_from_gfriends(
                    repo,
                    resolver,
                    actor_images_dir=settings.data_dir / "actor-images",
                    download=not args.cdn_only,
                    dry_run=args.dry_run,
                    limit=args.limit,
                    force_refresh_index=args.force_refresh,
                    http_client=http_client,
                )
    finally:
        resolver.close()
        if http_client is not None:
            http_client.close()

    print(
        f"scanned={stats.scanned} matched={stats.matched} filled={stats.filled} "
        f"no_match={stats.skipped_no_match} downloaded={stats.downloaded} "
        f"failed={stats.failed} dry_run={stats.dry_run} "
        f"filetree={stats.filetree_source}/{stats.filetree_entries}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
