"""Seed JavDB yearly TOP250 (2008+) from jinjier SQLite into local yearly JSON + catalog.

**Box → NAS sync** (do not fetch historical years on NAS):

1. On the box: export from ``/workspace/jinjier.sqlite3``
   (``scripts/export_javdb_yearly_top250.py`` → ``data/javranking/yearly/``).
2. Rsync yearly JSON to NAS shared data (``--sync-nas``).
3. Optionally upsert catalog tags with this script (``--lists-only`` / full seed).
4. NAS may refresh **current calendar year only** via ``SHADOW_MDC_PROXY_URL``.

Examples::

    PYTHONPATH=src .venv/bin/python scripts/export_javdb_yearly_top250.py \
      --sqlite /workspace/jinjier.sqlite3 --sync-nas

    PYTHONPATH=src .venv/bin/python scripts/seed_javdb_yearly_top250.py \
      --sqlite /workspace/jinjier.sqlite3 --years 2008-2024 --lists-only

Magnets are never auto-downloaded.
"""


from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.services.javdb_yearly_top250_seed import (  # noqa: E402
    DEFAULT_YEAR_END,
    DEFAULT_YEAR_START,
    list_available_years,
    seed_javdb_yearly_top250,
)


def _parse_years(raw: str | None) -> list[int] | None:
    if raw is None or not raw.strip():
        return None
    text = raw.strip()
    if "-" in text and "," not in text:
        start_s, end_s = text.split("-", 1)
        start, end = int(start_s), int(end_s)
        if end < start:
            raise SystemExit(f"invalid year range: {raw}")
        return list(range(start, end + 1))
    return [int(part.strip()) for part in text.split(",") if part.strip()]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=None,
        help="path to jinjier.sqlite3 (default: $JINJIER_SQLITE or data/jinjier.sqlite3)",
    )
    parser.add_argument(
        "--years",
        type=str,
        default=None,
        help="year range (2008-2024) or comma list; default: available 2008+ in sqlite",
    )
    parser.add_argument(
        "--limit-per-year",
        type=int,
        default=None,
        help="cap entries per year (testing)",
    )
    parser.add_argument(
        "--catalog-limit",
        type=int,
        default=None,
        help="cap total catalog upserts across years (testing)",
    )
    parser.add_argument(
        "--lists-only",
        action="store_true",
        help="write data/javranking/list-javdb-top250-*.json only; skip catalog",
    )
    parser.add_argument(
        "--catalog-only",
        action="store_true",
        help="upsert catalog works only; skip writing list JSON",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", type=str, default=None)
    arguments = parser.parse_args()

    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    sqlite_path = arguments.sqlite
    if sqlite_path is None:
        env_path = os.environ.get("JINJIER_SQLITE", "").strip()
        candidates = [
            Path(env_path) if env_path else None,
            settings.data_dir / "jinjier.sqlite3",
            Path("/tmp/jjdb/jinjier.sqlite3"),
            Path("/workspace/jinjier.sqlite3"),
        ]
        sqlite_path = next((path for path in candidates if path is not None and path.is_file()), None)
    if sqlite_path is None or not sqlite_path.is_file():
        raise SystemExit(
            "jinjier sqlite not found; pass --sqlite or set JINJIER_SQLITE "
            f"(looked under {settings.data_dir}/jinjier.sqlite3 and /tmp/jjdb/)"
        )

    years = _parse_years(arguments.years)
    available = list_available_years(sqlite_path)
    if years is None:
        years = [
            year
            for year in available
            if year >= DEFAULT_YEAR_START and (year <= DEFAULT_YEAR_END or year >= 2025)
        ] or available
    print(
        f"sqlite={sqlite_path} available_years={available} selected={years} "
        f"proxy={settings.proxy_url!r}"
    )

    database = Database(settings.database_url)
    database.initialize()
    with database.session() as session:
        repo = None if arguments.lists_only else Repository(session)
        result = seed_javdb_yearly_top250(
            sqlite_path=sqlite_path,
            data_dir=settings.data_dir,
            repo=repo,
            years=years,
            limit_per_year=arguments.limit_per_year,
            catalog_limit=arguments.catalog_limit,
            dry_run=arguments.dry_run,
            write_lists=not arguments.catalog_only,
            seed_catalog=not arguments.lists_only,
        )

    payload = result.model_dump(mode="json")
    if arguments.json:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        print(
            f"\nyears={list(result.years)} lists={list(result.lists_written)} "
            f"created={result.created} updated={result.updated} skipped={result.skipped}"
            + (" [dry-run]" if result.dry_run else "")
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
