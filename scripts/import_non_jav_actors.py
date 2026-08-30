import argparse
from pathlib import Path

from shadow_mdc.services.non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    parse_non_jav_actor_text,
)


def main() -> None:
    parser = argparse.ArgumentParser(description="Normalize a loose non-JAV actor list")
    parser.add_argument("source", type=Path, help="UTF-8 Markdown/CSV-like actor list")
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("data/non-jav-actors.json"),
        help="catalog output path",
    )
    arguments = parser.parse_args()
    catalog = parse_non_jav_actor_text(
        arguments.source.read_text(encoding="utf-8-sig"),
        source=arguments.source.name,
    )
    NonJavActorCatalogStore(arguments.output).save(catalog)
    match_names = sum(len(actor.match_names) for actor in catalog.actors)
    print(f"saved {len(catalog.actors)} actors and {match_names} safe match names to {arguments.output}")


if __name__ == "__main__":
    main()
