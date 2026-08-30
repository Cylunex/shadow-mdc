import argparse
from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.actor_catalog import (
    ActorCatalogStore,
    build_actor_catalog,
    merge_actor_catalogs,
)
from shadow_mdc.services.alias_store import IdentityAliasStore


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore durable actor knowledge from a database")
    parser.add_argument("database", type=Path)
    parser.add_argument("--output", type=Path, default=Path("data/actor-catalog.json"))
    parser.add_argument("--aliases", type=Path, default=Path("data/identity-aliases.json"))
    args = parser.parse_args()

    database_path = args.database.resolve()
    if not database_path.is_file():
        raise SystemExit(f"database does not exist: {database_path}")
    rules = IdentityAliasStore(args.aliases).load()
    database = Database(f"sqlite:///{database_path.as_posix()}")
    with database.session() as session:
        restored = build_actor_catalog(Repository(session).list_works(), rules)

    store = ActorCatalogStore(args.output)
    merged = merge_actor_catalogs(store.load(), restored, rules)
    store.save(merged)
    print(
        f"restored {len(merged)} actors and "
        f"{sum(profile.work_count for profile in merged)} actor-work references"
    )


if __name__ == "__main__":
    main()
