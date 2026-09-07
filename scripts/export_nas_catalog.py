"""Create a portable NAS catalog bundle without scan or review state.

Prefer ``scripts/export_catalog_bundle.py`` for incremental exports. This script
remains as a thin full-export wrapper for existing automation.
"""

from __future__ import annotations

import argparse
from pathlib import Path, PurePosixPath

from shadow_mdc.services.catalog_export import export_catalog_bundle


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export formal works, actors, relations and artwork without review state",
    )
    parser.add_argument("--source-data-dir", type=Path, default=Path("data"))
    parser.add_argument("--source-database", type=Path, default=Path("data/shadow-mdc.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--target-data-dir",
        required=True,
        help="absolute POSIX data path used by the destination service",
    )
    arguments = parser.parse_args()
    target_data_dir = PurePosixPath(arguments.target_data_dir)
    if not target_data_dir.is_absolute():
        raise SystemExit("--target-data-dir must be an absolute POSIX path")
    manifest = export_catalog_bundle(
        source_data_dir=arguments.source_data_dir,
        source_database=arguments.source_database,
        output=arguments.output,
        target_data_dir=target_data_dir,
        incremental=False,
    )
    print(
        f"exported catalog to {arguments.output}: "
        f"{manifest.catalog_counts}; omitted runtime state {manifest.omitted_runtime_counts}"
    )


if __name__ == "__main__":
    main()
