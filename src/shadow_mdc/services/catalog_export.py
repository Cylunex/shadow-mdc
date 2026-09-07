"""Export portable catalog bundles, with full or incremental modes."""

from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any

_PORTABLE_FILES = (
    "actor-catalog.json",
    "non-jav-actors.json",
    "non-jav-works.json",
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
    "collections",
    "work_collections",
)
_RUNTIME_TABLES = ("libraries", "media_assets", "match_candidates", "task_runs")
_STATE_VERSION = 1
DEFAULT_STATE_NAME = "export-manifest.json"


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
    mode: str
    catalog_counts: dict[str, int]
    omitted_runtime_counts: dict[str, int]
    files: tuple[FileDigest, ...]
    incremental: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ExportState:
    """Persisted fingerprints of the last successful export baseline."""

    version: int
    updated_at: str
    actors: dict[str, str]
    actor_images: dict[str, str]
    works: dict[str, str]
    artwork_files: dict[str, str]
    portable_files: dict[str, str]

    @classmethod
    def empty(cls) -> ExportState:
        return cls(
            version=_STATE_VERSION,
            updated_at="",
            actors={},
            actor_images={},
            works={},
            artwork_files={},
            portable_files={},
        )


def state_path(data_dir: Path) -> Path:
    return data_dir / DEFAULT_STATE_NAME


def load_export_state(path: Path) -> ExportState:
    if not path.is_file():
        return ExportState.empty()
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ExportState.empty()
    if not isinstance(raw, dict):
        return ExportState.empty()
    return ExportState(
        version=int(raw.get("version") or _STATE_VERSION),
        updated_at=str(raw.get("updated_at") or ""),
        actors=_string_map(raw.get("actors")),
        actor_images=_string_map(raw.get("actor_images")),
        works=_string_map(raw.get("works")),
        artwork_files=_string_map(raw.get("artwork_files")),
        portable_files=_string_map(raw.get("portable_files")),
    )


def save_export_state(path: Path, state: ExportState) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": state.version,
        "updated_at": state.updated_at,
        "actors": dict(sorted(state.actors.items())),
        "actor_images": dict(sorted(state.actor_images.items())),
        "works": dict(sorted(state.works.items())),
        "artwork_files": dict(sorted(state.artwork_files.items())),
        "portable_files": dict(sorted(state.portable_files.items())),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def compute_export_state(*, data_dir: Path, database: Path) -> ExportState:
    actors = _actor_fingerprints(data_dir / "non-jav-actors.json")
    actor_images = _directory_file_digests(data_dir / "actor-images")
    works = _work_fingerprints(database, artwork_root=data_dir / "artwork")
    artwork_files = _directory_file_digests(data_dir / "artwork")
    portable_files: dict[str, str] = {}
    for name in _PORTABLE_FILES:
        path = data_dir / name
        if path.is_file():
            portable_files[name] = _sha256(path)
    return ExportState(
        version=_STATE_VERSION,
        updated_at=datetime.now(UTC).isoformat(),
        actors=actors,
        actor_images=actor_images,
        works=works,
        artwork_files=artwork_files,
        portable_files=portable_files,
    )


def export_catalog_bundle(
    *,
    source_data_dir: Path,
    source_database: Path,
    output: Path,
    target_data_dir: PurePosixPath,
    incremental: bool = False,
    since: datetime | None = None,
    state_file: Path | None = None,
    update_state: bool = True,
) -> ExportManifest:
    """Export a portable catalog bundle.

    ``incremental`` / ``since`` only pack new or changed actors, images, works and artwork.
    Full export packs everything. After a successful export the local fingerprint state is
    updated so the next ``--since-last`` run can diff against it.
    """

    source_data_dir = source_data_dir.resolve()
    source_database = source_database.resolve()
    output = output.resolve()
    if not source_database.is_file():
        raise FileNotFoundError(f"source database does not exist: {source_database}")
    if output.exists():
        raise FileExistsError(f"output already exists: {output}")

    current = compute_export_state(data_dir=source_data_dir, database=source_database)
    baseline_path = state_file or state_path(source_data_dir)
    baseline = load_export_state(baseline_path) if (incremental or since is not None) else ExportState.empty()

    changed_actors, changed_images, changed_works, changed_artwork, changed_portable = _diff_state(
        current,
        baseline,
        since=since,
        data_dir=source_data_dir,
        database=source_database,
        force_all=not incremental and since is None,
    )

    mode = "full"
    if incremental:
        mode = "incremental"
    elif since is not None:
        mode = f"since:{since.isoformat()}"

    destination_data_dir = output / "data"
    destination_data_dir.mkdir(parents=True)

    _export_portable_files(
        source_data_dir=source_data_dir,
        destination_data_dir=destination_data_dir,
        changed_portable=changed_portable,
        changed_actors=changed_actors,
        changed_works=changed_works,
        full=mode == "full",
    )
    _export_media_subset(
        source_data_dir / "actor-images",
        destination_data_dir / "actor-images",
        selected=changed_images if mode != "full" else None,
    )
    _export_media_subset(
        source_data_dir / "artwork",
        destination_data_dir / "artwork",
        selected=changed_artwork if mode != "full" else None,
    )

    destination_database = destination_data_dir / "shadow-mdc.db"
    if mode == "full":
        _copy_database(source_database, destination_database)
        catalog_counts, omitted_counts = _clean_database(
            destination_database,
            source_data_dir=source_data_dir,
            target_data_dir=target_data_dir,
            keep_work_ids=None,
        )
    else:
        _copy_database(source_database, destination_database)
        catalog_counts, omitted_counts = _clean_database(
            destination_database,
            source_data_dir=source_data_dir,
            target_data_dir=target_data_dir,
            keep_work_ids=set(changed_works) if changed_works else set(),
        )

    incremental_meta = {
        "actors": sorted(changed_actors),
        "actor_images": sorted(changed_images),
        "works": sorted(changed_works),
        "artwork_files": sorted(changed_artwork),
        "portable_files": sorted(changed_portable),
        "baseline_updated_at": baseline.updated_at or None,
    }
    manifest = ExportManifest(
        created_at=datetime.now(UTC).isoformat(),
        source_database=str(source_database),
        target_data_dir=str(target_data_dir),
        mode=mode,
        catalog_counts=catalog_counts,
        omitted_runtime_counts=omitted_counts,
        files=_file_digests(output),
        incremental=incremental_meta if mode != "full" else {},
    )
    (output / "manifest.json").write_text(
        json.dumps(asdict(manifest), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    if update_state:
        save_export_state(baseline_path, current)
    return manifest


def _string_map(value: object) -> dict[str, str]:
    if not isinstance(value, dict):
        return {}
    return {str(key): str(item) for key, item in value.items() if item is not None}


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _directory_file_digests(root: Path) -> dict[str, str]:
    if not root.is_dir():
        return {}
    result: dict[str, str] = {}
    for path in sorted(root.rglob("*")):
        if path.is_file():
            result[path.relative_to(root).as_posix()] = _sha256(path)
    return result


def _actor_fingerprints(path: Path) -> dict[str, str]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    actors = payload.get("actors") if isinstance(payload, dict) else None
    if not isinstance(actors, list):
        return {}
    result: dict[str, str] = {}
    for item in actors:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        if not name:
            continue
        key = _actor_key(name)
        canonical = {
            "name": name,
            "aliases": sorted(str(alias) for alias in (item.get("aliases") or []) if alias),
            "groups": sorted(str(group) for group in (item.get("groups") or []) if group),
            "categories": sorted(str(category) for category in (item.get("categories") or []) if category),
            "match_names": sorted(str(alias) for alias in (item.get("match_names") or []) if alias),
            "image_file": item.get("image_file"),
            "biography": item.get("biography"),
            "notes": item.get("notes"),
        }
        result[key] = _sha256_text(json.dumps(canonical, ensure_ascii=False, sort_keys=True))
    return result


def _actor_key(name: str) -> str:
    return hashlib.sha256(name.encode("utf-8")).hexdigest()[:16]


def _work_fingerprints(database: Path, *, artwork_root: Path) -> dict[str, str]:
    if not database.is_file():
        return {}
    result: dict[str, str] = {}
    with sqlite3.connect(database) as connection:
        if not _table_exists(connection, "works"):
            return {}
        rows = connection.execute(
            "SELECT id, title, original_title, primary_code, studio, label, series, plot, "
            "actors, tags, artwork, updated_at FROM works"
        ).fetchall()
        for row in rows:
            work_id = str(row[0])
            artwork_hashes = _artwork_local_hashes(row[10], artwork_root=artwork_root)
            canonical = {
                "id": work_id,
                "title": row[1],
                "original_title": row[2],
                "primary_code": row[3],
                "studio": row[4],
                "label": row[5],
                "series": row[6],
                "plot": row[7],
                "actors": row[8],
                "tags": row[9],
                "artwork_hashes": artwork_hashes,
                "updated_at": row[11],
            }
            payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, default=str)
            result[work_id] = _sha256_text(payload)
    return result


def _artwork_local_hashes(encoded: object, *, artwork_root: Path) -> list[str]:
    try:
        artwork = json.loads(encoded or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    if not isinstance(artwork, list):
        return []
    digests: list[str] = []
    for item in artwork:
        if not isinstance(item, dict):
            continue
        local = item.get("local_path")
        if not isinstance(local, str) or not local:
            continue
        path = Path(local)
        if path.is_file():
            digests.append(_sha256(path))
            continue
        # Fall back to relative artwork path when absolute path differs across hosts.
        try:
            relative = path.relative_to(artwork_root) if path.is_absolute() else Path(local)
        except ValueError:
            relative = Path(path.name)
        candidate = artwork_root / relative
        if candidate.is_file():
            digests.append(_sha256(candidate))
    return sorted(digests)


def _diff_state(
    current: ExportState,
    baseline: ExportState,
    *,
    since: datetime | None,
    data_dir: Path,
    database: Path,
    force_all: bool,
) -> tuple[set[str], set[str], set[str], set[str], set[str]]:
    if force_all:
        return (
            set(current.actors),
            set(current.actor_images),
            set(current.works),
            set(current.artwork_files),
            set(current.portable_files),
        )

    changed_actors = {
        key for key, digest in current.actors.items() if baseline.actors.get(key) != digest
    }
    changed_images = {
        key for key, digest in current.actor_images.items() if baseline.actor_images.get(key) != digest
    }
    changed_works = {
        key for key, digest in current.works.items() if baseline.works.get(key) != digest
    }
    changed_artwork = {
        key for key, digest in current.artwork_files.items() if baseline.artwork_files.get(key) != digest
    }
    changed_portable = {
        key
        for key, digest in current.portable_files.items()
        if baseline.portable_files.get(key) != digest
    }

    if since is not None:
        since_works = _works_updated_since(database, since)
        changed_works |= since_works
        changed_images |= _files_mtime_since(data_dir / "actor-images", since)
        changed_artwork |= _files_mtime_since(data_dir / "artwork", since)
        # Actors linked to since-works via non-jav seed JSON are already covered by fingerprints;
        # also include actors referenced by those works' actor lists when possible.
        changed_actors |= _actors_for_works(database, since_works, actor_keys=set(current.actors))

    # Include artwork belonging to changed works even if file hash matched (path rewrite cases).
    changed_artwork |= _artwork_paths_for_works(database, changed_works, artwork_root=data_dir / "artwork")
    return changed_actors, changed_images, changed_works, changed_artwork, changed_portable


def _works_updated_since(database: Path, since: datetime) -> set[str]:
    if not database.is_file():
        return set()
    since_text = since.astimezone(UTC).isoformat()
    with sqlite3.connect(database) as connection:
        if not _table_exists(connection, "works"):
            return set()
        rows = connection.execute(
            "SELECT id, updated_at FROM works WHERE updated_at IS NOT NULL AND updated_at >= ?",
            (since_text,),
        ).fetchall()
        # SQLite may store naive timestamps; also compare lexicographically for ISO strings.
        result: set[str] = set()
        for work_id, updated_at in rows:
            result.add(str(work_id))
            _ = updated_at
        # Broader fallback: scan all and parse when possible.
        if not result:
            for work_id, updated_at in connection.execute("SELECT id, updated_at FROM works").fetchall():
                if _timestamp_at_or_after(updated_at, since):
                    result.add(str(work_id))
        return result


def _timestamp_at_or_after(value: object, since: datetime) -> bool:
    if value is None:
        return False
    text = str(value)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text >= since.astimezone(UTC).isoformat()
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed >= since


def _files_mtime_since(root: Path, since: datetime) -> set[str]:
    if not root.is_dir():
        return set()
    threshold = since.timestamp()
    return {
        path.relative_to(root).as_posix()
        for path in root.rglob("*")
        if path.is_file() and path.stat().st_mtime >= threshold
    }


def _actors_for_works(database: Path, work_ids: set[str], *, actor_keys: set[str]) -> set[str]:
    if not work_ids or not database.is_file():
        return set()
    names: set[str] = set()
    with sqlite3.connect(database) as connection:
        if not _table_exists(connection, "works"):
            return set()
        for work_id in work_ids:
            row = connection.execute("SELECT actors FROM works WHERE id = ?", (work_id,)).fetchone()
            if row is None:
                continue
            try:
                actors = json.loads(row[0] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if isinstance(actors, list):
                names.update(str(name).strip() for name in actors if str(name).strip())
    return {_actor_key(name) for name in names if _actor_key(name) in actor_keys}


def _artwork_paths_for_works(database: Path, work_ids: set[str], *, artwork_root: Path) -> set[str]:
    if not work_ids or not artwork_root.is_dir():
        return set()
    result: set[str] = set()
    # Prefer directory layout artwork/<work_id>/...
    for work_id in work_ids:
        work_dir = artwork_root / work_id
        if work_dir.is_dir():
            for path in work_dir.rglob("*"):
                if path.is_file():
                    result.add(path.relative_to(artwork_root).as_posix())
    if not database.is_file():
        return result
    with sqlite3.connect(database) as connection:
        if not _table_exists(connection, "works"):
            return result
        for work_id in work_ids:
            row = connection.execute("SELECT artwork FROM works WHERE id = ?", (work_id,)).fetchone()
            if row is None:
                continue
            try:
                artwork = json.loads(row[0] or "[]")
            except (TypeError, json.JSONDecodeError):
                continue
            if not isinstance(artwork, list):
                continue
            for item in artwork:
                if not isinstance(item, dict):
                    continue
                local = item.get("local_path")
                if not isinstance(local, str) or not local:
                    continue
                path = Path(local)
                try:
                    relative = path.resolve().relative_to(artwork_root.resolve())
                    result.add(relative.as_posix())
                except Exception:
                    if (artwork_root / path.name).is_file():
                        result.add(path.name)
    return result


def _export_portable_files(
    *,
    source_data_dir: Path,
    destination_data_dir: Path,
    changed_portable: set[str],
    changed_actors: set[str],
    changed_works: set[str],
    full: bool,
) -> None:
    for name in _PORTABLE_FILES:
        source = source_data_dir / name
        if not source.is_file():
            continue
        if not full and name not in changed_portable and name not in {
            "non-jav-actors.json",
            "non-jav-works.json",
        }:
            continue
        if name == "non-jav-actors.json" and not full:
            _write_filtered_actors(source, destination_data_dir / name, changed_actors)
            continue
        if name == "non-jav-works.json" and not full:
            _write_filtered_works(source, destination_data_dir / name, changed_works)
            continue
        shutil.copy2(source, destination_data_dir / name)


def _write_filtered_actors(source: Path, destination: Path, changed_actor_keys: set[str]) -> None:
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("actors"), list):
        shutil.copy2(source, destination)
        return
    filtered = [
        item
        for item in payload["actors"]
        if isinstance(item, dict) and _actor_key(str(item.get("name") or "")) in changed_actor_keys
    ]
    output = dict(payload)
    output["actors"] = filtered
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _write_filtered_works(source: Path, destination: Path, changed_work_ids: set[str]) -> None:
    try:
        payload = json.loads(source.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(payload, dict) or not isinstance(payload.get("works"), list):
        shutil.copy2(source, destination)
        return
    filtered = [
        item
        for item in payload["works"]
        if isinstance(item, dict) and str(item.get("id") or "") in changed_work_ids
    ]
    output = dict(payload)
    output["works"] = filtered
    destination.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _export_media_subset(source: Path, destination: Path, *, selected: set[str] | None) -> None:
    destination.mkdir(parents=True, exist_ok=True)
    if not source.is_dir():
        return
    if selected is None:
        shutil.copytree(source, destination, dirs_exist_ok=True)
        return
    for relative in sorted(selected):
        src = source / relative
        if not src.is_file():
            continue
        dest = destination / relative
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dest)


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
    keep_work_ids: set[str] | None,
) -> tuple[dict[str, int], dict[str, int]]:
    with sqlite3.connect(database) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        omitted_counts = _table_counts(connection, _RUNTIME_TABLES)
        for table in ("match_candidates", "media_assets", "libraries", "task_runs"):
            if _table_exists(connection, table):
                connection.execute(f'DELETE FROM "{table}"')
        if keep_work_ids is not None and _table_exists(connection, "works"):
            if keep_work_ids:
                placeholders = ",".join("?" for _ in keep_work_ids)
                connection.execute(
                    f"DELETE FROM works WHERE id NOT IN ({placeholders})",
                    tuple(keep_work_ids),
                )
            else:
                connection.execute("DELETE FROM works")
            # Drop orphan actors not referenced by remaining work_actors.
            if _table_exists(connection, "actors") and _table_exists(connection, "work_actors"):
                connection.execute(
                    """
                    DELETE FROM actors
                    WHERE id NOT IN (SELECT DISTINCT actor_id FROM work_actors)
                    """
                )
            if _table_exists(connection, "collections") and _table_exists(connection, "work_collections"):
                connection.execute(
                    """
                    DELETE FROM collections
                    WHERE id NOT IN (SELECT DISTINCT collection_id FROM work_collections)
                    """
                )
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


__all__ = [
    "DEFAULT_STATE_NAME",
    "ExportManifest",
    "ExportState",
    "FileDigest",
    "compute_export_state",
    "export_catalog_bundle",
    "load_export_state",
    "save_export_state",
    "state_path",
]
