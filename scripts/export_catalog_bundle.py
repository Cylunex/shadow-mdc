#!/usr/bin/env python3
"""Export a portable catalog bundle (full or incremental).

Complements ``import_catalog_bundle`` / ``export_nas_catalog``. Incremental mode
exports only actors, images, works and artwork that changed since the last export
fingerprint state (``data/export-manifest.json``).
"""

from __future__ import annotations

import argparse
from datetime import datetime
from pathlib import Path, PurePosixPath

from shadow_mdc.services.catalog_export import export_catalog_bundle, state_path


def _parse_since(value: str) -> datetime:
    text = value.strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"invalid ISO datetime: {value}") from exc
    return parsed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source-data-dir", type=Path, default=Path("data"))
    parser.add_argument("--source-database", type=Path, default=Path("data/shadow-mdc.db"))
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--target-data-dir",
        required=True,
        help="absolute POSIX data path used by the destination service",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--incremental",
        "--since-last",
        dest="incremental",
        action="store_true",
        help="export only items changed since data/export-manifest.json",
    )
    mode.add_argument(
        "--since",
        type=_parse_since,
        default=None,
        help="export items changed at or after this ISO datetime (also updates fingerprint state)",
    )
    parser.add_argument(
        "--state-file",
        type=Path,
        default=None,
        help="fingerprint state path (default: <source-data-dir>/export-manifest.json)",
    )
    parser.add_argument(
        "--no-update-state",
        action="store_true",
        help="do not rewrite the local export fingerprint state after packing",
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
        incremental=arguments.incremental,
        since=arguments.since,
        state_file=arguments.state_file or state_path(arguments.source_data_dir),
        update_state=not arguments.no_update_state,
    )
    extra = ""
    if manifest.mode != "full" and manifest.incremental:
        extra = (
            f"; delta actors={len(manifest.incremental.get('actors', []))} "
            f"images={len(manifest.incremental.get('actor_images', []))} "
            f"works={len(manifest.incremental.get('works', []))} "
            f"artwork={len(manifest.incremental.get('artwork_files', []))}"
        )
    print(
        f"exported catalog ({manifest.mode}) to {arguments.output}: "
        f"{manifest.catalog_counts}; omitted runtime state {manifest.omitted_runtime_counts}{extra}"
    )


if __name__ == "__main__":
    main()
