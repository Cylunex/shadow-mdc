#!/usr/bin/env python3
"""Export JavDB yearly TOP250 into local static JSON (box → NAS sync).

Historical years come from jinjier SQLite on this box. Do **not** run historical
fetches on NAS — rsync ``data/javranking/yearly/`` after export.

Example::

    PYTHONPATH=src .venv/bin/python scripts/export_javdb_yearly_top250.py
    PYTHONPATH=src .venv/bin/python scripts/export_javdb_yearly_top250.py \\
        --sqlite /workspace/jinjier.sqlite3 --force
    PYTHONPATH=src .venv/bin/python scripts/export_javdb_yearly_top250.py \\
        --from-index-years 2025 --sync-nas
"""

from __future__ import annotations

import argparse
import os
import subprocess
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.services.javdb_yearly_top250 import (  # noqa: E402
    current_calendar_year,
    export_historical_from_jinjier,
    export_year_from_search_index,
    last_completed_year,
    list_local_yearly_years,
    yearly_dir,
)


def _sync_yearly_to_nas(*, data_dir: Path, dry_run: bool) -> int:
    """Rsync only yearly JSON blobs to NAS shared data (no remote fetch on NAS)."""

    source = yearly_dir(data_dir)
    if not source.is_dir():
        print(f"yearly dir missing: {source}", file=sys.stderr)
        return 2
    nas_host = os.environ.get("NAS_HOST", "nas")
    nas_dest = os.environ.get(
        "NAS_YEARLY_DEST",
        "/data/project/shadow-mdc/shared/data/javranking/yearly/",
    )
    cmd = [
        "rsync",
        "-a",
        "--mkpath",
        f"{source}/",
        f"{nas_host}:{nas_dest}",
    ]
    if dry_run:
        cmd.insert(1, "--dry-run")
    print(f"==> rsync yearly JSON → {nas_host}:{nas_dest}")
    print(" ".join(cmd))
    completed = subprocess.run(cmd, check=False)
    return int(completed.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--sqlite",
        type=Path,
        default=Path("/workspace/jinjier.sqlite3"),
        help="jinjier.sqlite3 path (box-local; default /workspace/jinjier.sqlite3)",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="shadow-mdc data dir (default from settings)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="overwrite existing frozen yearly JSON files",
    )
    parser.add_argument(
        "--years",
        type=int,
        nargs="*",
        default=None,
        help="limit jinjier export to these years",
    )
    parser.add_argument(
        "--from-index-years",
        type=int,
        nargs="*",
        default=None,
        help="also materialize these years from local search-index.json",
    )
    parser.add_argument(
        "--include-current-from-index",
        action="store_true",
        help="export current calendar year from local search-index if present",
    )
    parser.add_argument(
        "--sync-nas",
        action="store_true",
        help="rsync data/javranking/yearly/ to NAS shared data after export",
    )
    parser.add_argument(
        "--dry-run-sync",
        action="store_true",
        help="with --sync-nas, pass --dry-run to rsync",
    )
    arguments = parser.parse_args()

    settings = Settings()
    if arguments.data_dir is not None:
        settings = settings.model_copy(update={"data_dir": arguments.data_dir})
    settings.ensure_directories()
    data_dir = Path(settings.data_dir)
    today = date.today()
    print(
        f"export yearly TOP250 → {yearly_dir(data_dir)} "
        f"(today={today.isoformat()} completed≤{last_completed_year(today=today)} "
        f"current={current_calendar_year(today=today)})"
    )

    written = export_historical_from_jinjier(
        arguments.sqlite,
        data_dir,
        force=arguments.force,
        years=arguments.years,
        today=today,
    )
    for listing in written:
        print(
            f"  jinjier {listing.year}: {len(listing.items)} items "
            f"rev={listing.revision} frozen={listing.frozen}"
        )

    index_years = list(arguments.from_index_years or [])
    if arguments.include_current_from_index:
        index_years.append(current_calendar_year(today=today))
    # Default: fill any completed year missing on disk from local search-index
    # (e.g. 2025 when jinjier only has ≤2024).
    if arguments.from_index_years is None and not arguments.include_current_from_index:
        local_years = set(list_local_yearly_years(data_dir))
        completed = last_completed_year(today=today)
        for year in range(2008, completed + 1):
            if year not in local_years:
                index_years.append(year)

    for year in sorted(set(index_years)):
        listing = export_year_from_search_index(
            data_dir,
            year,
            force=arguments.force,
            today=today,
        )
        if listing is None:
            print(f"  index {year}: skipped (no local search-index entries)")
            continue
        print(
            f"  index  {listing.year}: {len(listing.items)} items "
            f"rev={listing.revision} frozen={listing.frozen} source={listing.source}"
        )

    years = list_local_yearly_years(data_dir)
    print(f"local yearly files: {years} ({len(years)} years)")

    if arguments.sync_nas:
        return _sync_yearly_to_nas(data_dir=data_dir, dry_run=arguments.dry_run_sync)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
