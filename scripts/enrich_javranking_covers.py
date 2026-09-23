#!/usr/bin/env python3
"""Backfill missing cover_url on curated JavRanking video lists (NAS-safe).

Fills ``data/javranking/list-*.json`` cover fields from the local search-index.
Does not push to GitHub. Prefer running on NAS against shared data.

Example::

    PYTHONPATH=src .venv/bin/python scripts/enrich_javranking_covers.py --dry-run
    PYTHONPATH=src .venv/bin/python scripts/enrich_javranking_covers.py \\
        --data-dir /data/project/shadow-mdc/shared/data
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.services.javranking_client import (  # noqa: E402
    CURATED_LIST_SLUGS,
    JavRankingIndexCache,
    curated_list_kind,
    enrich_curated_videos_with_covers,
)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=ROOT / "data",
        help="shadow-mdc data directory (default: ./data)",
    )
    parser.add_argument(
        "--slug",
        action="append",
        dest="slugs",
        help="curated list slug (repeatable); default: video curated lists",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--persist-enriched-cache",
        action="store_true",
        help="rewrite list-*.json via JavRankingIndexCache after enrichment",
    )
    args = parser.parse_args()

    data_dir: Path = args.data_dir
    cache_dir = data_dir / "javranking"
    cache = JavRankingIndexCache(cache_dir)
    index = cache.read_cached_index()
    if index is None or not index.videos:
        print(f"ERROR: missing search-index under {cache_dir}", file=sys.stderr)
        return 2

    slugs = tuple(args.slugs) if args.slugs else tuple(
        slug for slug in CURATED_LIST_SLUGS if curated_list_kind(slug) == "videos"
    )
    total_filled = 0
    for slug in slugs:
        curated = cache.read_curated_list(slug)
        if curated is None:
            print(f"skip {slug}: list file missing")
            continue
        if curated.kind != "videos":
            print(f"skip {slug}: kind={curated.kind}")
            continue
        before_missing = sum(1 for item in curated.videos if not item.cover_url)
        enriched_videos = enrich_curated_videos_with_covers(curated.videos, index.videos)
        after_missing = sum(1 for item in enriched_videos if not item.cover_url)
        filled = before_missing - after_missing
        total_filled += filled
        print(
            f"{slug}: videos={len(curated.videos)} missing {before_missing} → {after_missing} "
            f"(filled {filled})"
        )
        if filled <= 0:
            continue
        updated = curated.model_copy(update={"videos": enriched_videos})
        if args.dry_run:
            continue
        if args.persist_enriched_cache:
            cache.write_curated_list(updated)
        else:
            # Preserve on-disk shape used by existing caches (cover_url field).
            path = cache.list_cache_path(slug)
            payload = json.loads(path.read_text(encoding="utf-8"))
            covers = {item.position: item.cover_url for item in enriched_videos}
            for row in payload.get("videos") or []:
                pos = row.get("position")
                if pos in covers and covers[pos] and not row.get("cover_url"):
                    row["cover_url"] = covers[pos]
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(f"  wrote {path}")
    print(f"total filled: {total_filled}" + (" (dry-run)" if args.dry_run else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
