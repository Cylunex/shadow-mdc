#!/usr/bin/env python3
"""Back up + remove catalog works created solely by r18dump enrich.

A work is removed only when ALL hold:
  - every ``field_sources`` value is ``r18dump``
  - ``created_at`` date >= --since (default 2026-09-24)
  - no rows in work_magnets / media_assets / pan_offline_tasks
  - no external_identities outside {r18dump, global}
  - no source_snapshots outside {r18dump}

Example::

    PYTHONPATH=src .venv/bin/python scripts/cleanup_r18dump_orphan_works.py \\
      --database /data/project/shadow-mdc/shared/data/shadow-mdc.db --dry-run
    PYTHONPATH=src .venv/bin/python scripts/cleanup_r18dump_orphan_works.py \\
      --database /data/project/shadow-mdc/shared/data/shadow-mdc.db --apply
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date, datetime
from pathlib import Path


def _only_r18dump(sources_raw: str | None) -> bool:
    try:
        sources = json.loads(sources_raw or "{}")
    except json.JSONDecodeError:
        return False
    if not isinstance(sources, dict) or not sources:
        return False
    values = {str(value) for value in sources.values() if value}
    return bool(values) and values <= {"r18dump"}


def _created_date(value: object) -> date | None:
    if value is None:
        return None
    text = str(value).replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def _row_dict(cursor: sqlite3.Cursor, row: sqlite3.Row) -> dict[str, object]:
    return {key: row[key] for key in row.keys()}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--since", default="2026-09-24")
    parser.add_argument("--backup", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--apply", action="store_true")
    arguments = parser.parse_args()
    if arguments.dry_run == arguments.apply:
        print("pass exactly one of --dry-run / --apply", file=sys.stderr)
        return 2

    since = date.fromisoformat(arguments.since)
    database = arguments.database
    if not database.is_file():
        print(f"database missing: {database}", file=sys.stderr)
        return 2
    backup_path = arguments.backup or (
        database.parent / "r18-dumps" / f"orphan-works-backup-{since.isoformat()}.json"
    )

    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    works = list(connection.execute("SELECT * FROM works"))
    remove_rows: list[sqlite3.Row] = []
    keep: list[dict[str, object]] = []

    for work in works:
        if not _only_r18dump(work["field_sources"]):
            continue
        created = _created_date(work["created_at"])
        if created is None or created < since:
            keep.append({"id": work["id"], "code": work["primary_code"], "reason": "before_since"})
            continue
        work_id = work["id"]
        reasons: list[str] = []
        if connection.execute(
            "SELECT 1 FROM work_magnets WHERE work_id = ? LIMIT 1", (work_id,)
        ).fetchone():
            reasons.append("magnets")
        if connection.execute(
            "SELECT 1 FROM media_assets WHERE work_id = ? LIMIT 1", (work_id,)
        ).fetchone():
            reasons.append("media_assets")
        if connection.execute(
            "SELECT 1 FROM pan_offline_tasks WHERE work_id = ? LIMIT 1", (work_id,)
        ).fetchone():
            reasons.append("pan_offline")
        foreign_identity = connection.execute(
            """
            SELECT provider FROM external_identities
            WHERE work_id = ? AND provider NOT IN ('r18dump', 'global')
            LIMIT 1
            """,
            (work_id,),
        ).fetchone()
        if foreign_identity is not None:
            reasons.append(f"identity:{foreign_identity['provider']}")
        foreign_snap = connection.execute(
            """
            SELECT provider FROM source_snapshots
            WHERE work_id = ? AND provider != 'r18dump'
            LIMIT 1
            """,
            (work_id,),
        ).fetchone()
        if foreign_snap is not None:
            reasons.append(f"snapshot:{foreign_snap['provider']}")
        if reasons:
            keep.append({"id": work_id, "code": work["primary_code"], "reason": ",".join(reasons)})
        else:
            remove_rows.append(work)

    payload = {
        "database": str(database),
        "since": since.isoformat(),
        "removed_count": len(remove_rows),
        "kept_count": len(keep),
        "kept": keep,
        "removed": [_row_dict(connection.execute("SELECT 1"), row) for row in remove_rows],
    }
    # row_dict helper above is awkward — rebuild cleanly
    payload["removed"] = [{key: row[key] for key in row.keys()} for row in remove_rows]
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    backup_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str) + "\n", encoding="utf-8")
    print(f"Backup: {backup_path}")
    print(json.dumps({"remove": len(remove_rows), "keep": len(keep), "dry_run": arguments.dry_run}, indent=2))

    if arguments.dry_run:
        connection.close()
        return 0

    child_tables = (
        "work_magnets",
        "work_actors",
        "work_collections",
        "external_identities",
        "source_snapshots",
        "pan_offline_tasks",
    )
    try:
        connection.execute("BEGIN IMMEDIATE")
        for row in remove_rows:
            work_id = row["id"]
            for table in child_tables:
                connection.execute(f"DELETE FROM {table} WHERE work_id = ?", (work_id,))
            connection.execute("UPDATE media_assets SET work_id = NULL WHERE work_id = ?", (work_id,))
            connection.execute("DELETE FROM works WHERE id = ?", (work_id,))
        connection.commit()
    except Exception:
        connection.rollback()
        raise
    finally:
        connection.close()
    print(json.dumps({"applied": True, "removed": len(remove_rows), "kept": len(keep)}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
