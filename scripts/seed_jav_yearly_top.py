#!/usr/bin/env python3
"""Seed curated FANZA yearly top-10 JAV actresses (2006–2025) into SQLite."""

from __future__ import annotations

import argparse
from pathlib import Path

from shadow_mdc.config import Settings
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.jav_yearly_seed import load_jav_yearly_seed, seed_jav_yearly_top


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=Path,
        default=None,
        help="path to jav-yearly-top.json (default: data/jav-yearly-top.json)",
    )
    parser.add_argument(
        "--no-posters",
        action="store_true",
        help="skip downloading DMM/public cover images",
    )
    arguments = parser.parse_args()
    settings = Settings()
    settings.ensure_directories()
    default_seed = settings.data_dir / "jav-yearly-top.json"
    seed_path = arguments.seed or default_seed
    if not seed_path.is_file():
        raise SystemExit(f"seed file not found: {seed_path}")

    catalog = load_jav_yearly_seed(seed_path)
    actress_count = len({a.name for y in catalog.years for a in y.actresses})
    work_count = len({w.code for y in catalog.years for a in y.actresses for w in a.works})
    print(
        f"loading {seed_path}: years={len(catalog.years)} "
        f"actress_entries={sum(len(y.actresses) for y in catalog.years)} "
        f"unique_actresses={actress_count} unique_works={work_count}"
    )

    database = Database(settings.database_url)
    database.initialize()
    with database.session() as session:
        result = seed_jav_yearly_top(
            Repository(session),
            seed_path=seed_path,
            artwork_dir=settings.data_dir / "artwork",
            download_posters=not arguments.no_posters,
            artwork_max_bytes=settings.artwork_max_bytes,
        )
    print(
        f"seeded JAV yearly top from {seed_path}: "
        f"years={list(result.years)} created={result.created} updated={result.updated} "
        f"posters={result.posters} actresses={result.actresses} works={result.works}"
    )


if __name__ == "__main__":
    main()
