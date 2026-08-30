"""Create a portable NAS catalog bundle without scan or review state."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath

_PORTABLE_FILES = (
    "actor-catalog.json",
    "non-jav-actors.json",
    "filter-words.txt",
    "identity-aliases.json",
    "translations.db",
)
_PORTABLE_DIRECTORIES = ("artwork", "actor-images")
_CATALOG_TABLES = (
    "works",
    "actors",
    "work_actors",
    "external_identities",
    "source_snapshots",
)
_RUNTIME_TABLES = ("libraries", "media_assets", "match_candidates", "task_runs")


@dataclass(frozen=True)
class FileDigest:
    path: str
    size: int
    sha256: str


@dataclass(frozen=True)
class ExportManifest:
    created_at: str
    source_database: str
    target_data_dir: str
    catalog_counts: dict[str, int]
    omitted_runtime_counts: dict[str, int]
    files: tuple[FileDigest, ...]


def _table_exists(connection: sqlite3.Connection, table: str) -> bool:
    row = connection.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
        (table,),
    ).fetchone()
    return row is not None


def _table_counts(connection: sqlite3.Connection, tables: tuple[str, ...]) -> dict[str, int]:
    return {
        table: int(connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0])
        for table in tables
        if _table_exists(connection, table)
    }


def _portable_artwork_path(
    value: object,
    *,
    source_data_dir: Path,
    target_data_dir: PurePosixPath,
) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    candidate = Path(value)
    try:
        relative = candidate.resolve().relative_to(source_data_dir)
    except ValueError:
        return None
    return str(target_data_dir.joinpath(*relative.parts))


def _rewrite_artwork_paths(
    connection: sqlite3.Connection,
    *,
    source_data_dir: Path,
    target_data_dir: PurePosixPath,
) -> None:
    if not _table_exists(connection, "works"):
        return
    rows = connection.execute("SELECT id, artwork FROM works").fetchall()
    for work_id, encoded_artwork in rows:
        try:
            artwork = json.loads(encoded_artwork or "[]")
        except (TypeError, json.JSONDecodeError):
            continue
        if not isinstance(artwork, list):
            continue
        changed = False
        for item in artwork:
            if not isinstance(item, dict) or "local_path" not in item:
                continue
            portable = _portable_artwork_path(
                item.get("local_path"),
                source_data_dir=source_data_dir,
                target_data_dir=target_data_dir,
            )
            if portable is None:
                item.pop("local_path", None)
            else:
                item["local_path"] = portable
            changed = True
        if changed:
            connection.execute(
                "UPDATE works SET artwork = ? WHERE id = ?",
                (json.dumps(artwork, ensure_ascii=False), work_id),
            )


def _copy_database(source: Path, destination: Path) -> None:
    with sqlite3.connect(source) as source_connection:
        source_connection.execute("PRAGMA busy_timeout = 30000")
        with sqlite3.connect(destination) as destination_connection:
            source_connection.backup(destination_connection)


def _clean_database(
    database: Path,
    *,
    source_data_dir: Path,
    target_data_dir: PurePosixPath,
) -> tuple[dict[str, int], dict[str, int]]:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        omitted_counts = _table_counts(connection, _RUNTIME_TABLES)
        for table in ("match_candidates", "media_assets", "libraries", "task_runs"):
            if _table_exists(connection, table):
                connection.execute(f'DELETE FROM "{table}"')
        _rewrite_artwork_paths(
            connection,
            source_data_dir=source_data_dir,
            target_data_dir=target_data_dir,
        )
        connection.commit()
        violations = connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise RuntimeError(f"portable database has foreign-key violations: {violations[:5]}")
        runtime_after = _table_counts(connection, _RUNTIME_TABLES)
        if any(runtime_after.values()):
            raise RuntimeError(f"runtime tables were not emptied: {runtime_after}")
        catalog_counts = _table_counts(connection, _CATALOG_TABLES)
        connection.execute("VACUUM")
        connection.execute("PRAGMA journal_mode = DELETE")
    return catalog_counts, omitted_counts


def _copy_portable_data(source_data_dir: Path, destination_data_dir: Path) -> None:
    destination_data_dir.mkdir(parents=True)
    for filename in _PORTABLE_FILES:
        source = source_data_dir / filename
        if source.is_file():
            shutil.copy2(source, destination_data_dir / filename)
    for directory in _PORTABLE_DIRECTORIES:
        source = source_data_dir / directory
        destination = destination_data_dir / directory
        if source.is_dir():
            shutil.copytree(source, destination)
        else:
            destination.mkdir()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _file_digests(root: Path) -> tuple[FileDigest, ...]:
    return tuple(
        FileDigest(
            path=path.relative_to(root).as_posix(),
            size=path.stat().st_size,
            sha256=_sha256(path),
        )
        for path in sorted(root.rglob("*"))
        if path.is_file() and path.name != "manifest.json"
    )


def export_catalog_bundle(
    *,
    source_data_dir: Path,
    source_database: Path,
    output: Path,
    target_data_dir: PurePosixPath,
) -> ExportManifest:
    source_data_dir = source_data_dir.resolve()
    source_database = source_database.resolve()
    output = output.resolve()
    if not source_database.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_database}")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    destination_data_dir = output / "data"
    _copy_portable_data(source_data_dir, destination_data_dir)
    destination_database = destination_data_dir / "shadow-mdc.db"
    _copy_database(source_database, destination_database)
    catalog_counts, omitted_counts = _clean_database(
        destination_database,
        source_data_dir=source_data_dir,
        target_data_dir=target_data_dir,
    )
    manifest = ExportManifest(
        created_at=datetime.now(UTC).isoformat(),
        source_database=str(source_database),
        target_data_dir=str(target_data_dir),
        catalog_counts=catalog_counts,
        omitted_runtime_counts=omitted_counts,
        files=_file_digests(output),
    )
    (output / "manifest.json").write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    return manifest


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
    )
    print(
        f"exported catalog to {arguments.output}: "
        f"{manifest.catalog_counts}; omitted runtime state {manifest.omitted_runtime_counts}"
    )


if __name__ == "__main__":
    main()
