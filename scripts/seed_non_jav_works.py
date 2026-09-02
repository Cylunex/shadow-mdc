#!/usr/bin/env python3
"""Load curated non-JAV filmography into the local SQLite works database."""

from __future__ import annotations

import argparse
from pathlib import Path

from shadow_mdc.config import Settings
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.non_jav_actor_catalog import NonJavActorCatalogStore
from shadow_mdc.services.non_jav_work_seed import seed_non_jav_works


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--seed",
        type=Path,
        default=None,
        help="path to non-jav-works.json (default: <data_dir>/non-jav-works.json)",
    )
    arguments = parser.parse_args()
    settings = Settings()
    settings.ensure_directories()
    seed_path = arguments.seed or (settings.data_dir / "non-jav-works.json")
    database = Database(settings.database_url)
    database.initialize()
    actor_store = NonJavActorCatalogStore(settings.data_dir / "non-jav-actors.json")
    with database.session() as session:
        result = seed_non_jav_works(
            Repository(session),
            seed_path=seed_path,
            actor_store=actor_store,
            actor_images_dir=settings.data_dir / "actor-images",
            artwork_dir=settings.data_dir / "artwork",
        )
    print(
        f"seeded non-JAV works from {seed_path}: "
        f"created={result.created} updated={result.updated} "
        f"posters={result.posters} actors_added={result.actors_added}"
    )


if __name__ == "__main__":
    main()
