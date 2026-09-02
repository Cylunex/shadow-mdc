#!/usr/bin/env python3
"""Merge-import a portable catalog bundle into local data/ without wiping runtime state."""

from __future__ import annotations

import argparse
from pathlib import Path

from shadow_mdc.config import Settings
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.actor_catalog import ActorCatalogStore
from shadow_mdc.services.alias_store import IdentityAliasStore
from shadow_mdc.services.catalog_import import CatalogImportRequest, import_catalog_bundle
from shadow_mdc.services.non_jav_actor_catalog import NonJavActorCatalogStore
from shadow_mdc.services.path_filter import FilterWordsStore


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--bundle",
        type=Path,
        required=True,
        help="directory, .tar.gz, or .zip of local-data package or export_nas_catalog output",
    )
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="destination data directory (default: SHADOW_MDC_DATA_DIR / Settings.data_dir)",
    )
    parser.add_argument(
        "--database-url",
        default=None,
        help="SQLAlchemy URL for works DB (default: Settings.database_url)",
    )
    parser.add_argument("--dry-run", action="store_true", help="report merges without writing")
    parser.add_argument("--actors-only", action="store_true", help="only merge actor catalogs/images")
    parser.add_argument("--works-only", action="store_true", help="only seed/import works and artwork")
    parser.add_argument(
        "--skip-formal",
        action="store_true",
        help="skip JAV actor-catalog / identity-aliases / filter-words / portable DB works",
    )
    parser.add_argument("--json", action="store_true", help="print machine-readable result JSON")
    arguments = parser.parse_args()

    settings = Settings()
    if arguments.data_dir is not None:
        settings = settings.model_copy(update={"data_dir": arguments.data_dir})
    if arguments.database_url is not None:
        settings = settings.model_copy(update={"database_url": arguments.database_url})
    settings.ensure_directories()

    database = Database(settings.database_url)
    database.initialize()
    actor_store = NonJavActorCatalogStore(settings.data_dir / "non-jav-actors.json")
    request = CatalogImportRequest(
        dry_run=arguments.dry_run,
        actors_only=arguments.actors_only,
        works_only=arguments.works_only,
        include_formal=not arguments.skip_formal,
    )
    with database.session() as session:
        result = import_catalog_bundle(
            bundle=arguments.bundle,
            data_dir=settings.data_dir,
            repo=Repository(session),
            actor_store=actor_store,
            actor_images_dir=settings.data_dir / "actor-images",
            artwork_dir=settings.data_dir / "artwork",
            request=request,
            actor_catalog_store=ActorCatalogStore(settings.data_dir / "actor-catalog.json"),
            alias_store=IdentityAliasStore(settings.data_dir / "identity-aliases.json"),
            filter_words_store=FilterWordsStore(settings.data_dir / "filter-words.txt"),
        )

    if arguments.json:
        print(result.model_dump_json(indent=2))
    else:
        mode = "dry-run" if result.dry_run else "imported"
        print(
            f"{mode} {result.bundle_kind} from {arguments.bundle}: "
            f"actors +{result.actors_added}/~{result.actors_updated}/= {result.actors_unchanged}, "
            f"images {result.actor_images_copied}, "
            f"works +{result.works_created}/~{result.works_updated} "
            f"(posters {result.works_posters}, seed actors {result.works_actors_added}), "
            f"artwork files {result.artwork_copied}, "
            f"formal works {result.formal_works_imported}, "
            f"jav actors +{result.jav_actors_merged}, "
            f"alias keys +{result.aliases_keys_added}, "
            f"filter words +{result.filter_words_added}"
        )
        for note in result.notes:
            print(f"note: {note}")


if __name__ == "__main__":
    main()
