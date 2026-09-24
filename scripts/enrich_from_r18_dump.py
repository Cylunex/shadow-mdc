#!/usr/bin/env python3
"""Fill JAV catalog gaps from the offline r18.dev SQLite dump.

Strictly update-only — never creates works. Uses ``Repository.merge_provider_into_work``
on existing catalog candidates only.

Example::

    PYTHONPATH=src .venv/bin/python scripts/enrich_from_r18_dump.py --limit 500
    PYTHONPATH=src .venv/bin/python scripts/enrich_from_r18_dump.py --codes SSIS-001,SONE-118
    PYTHONPATH=src .venv/bin/python scripts/enrich_from_r18_dump.py --year 2021 --limit 300
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.models import Work  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.enums import ContentFamily  # noqa: E402
from shadow_mdc.identity import extract_code  # noqa: E402
from shadow_mdc.services.actress_names import normalize_actress_key  # noqa: E402
from shadow_mdc.services.r18_dump import R18DumpStore  # noqa: E402

TRACK_FIELDS = (
    "title",
    "original_title",
    "actors",
    "release_date",
    "runtime_seconds",
    "studio",
    "label",
    "series",
    "plot",
    "original_plot",
    "tags",
    "artwork",
)


def _field_present(work: Work, name: str) -> bool:
    if name == "title":
        return bool((work.title or "").strip())
    if name == "original_title":
        return bool((work.original_title or "").strip())
    if name == "actors":
        return bool(work.actors)
    if name == "release_date":
        return work.release_date is not None
    if name == "runtime_seconds":
        return bool(work.runtime_seconds)
    if name == "studio":
        return bool(work.studio)
    if name == "label":
        return bool(work.label)
    if name == "series":
        return bool(work.series)
    if name == "plot":
        return bool((work.plot or "").strip())
    if name == "original_plot":
        return bool((work.original_plot or "").strip())
    if name == "tags":
        return bool(work.tags)
    if name == "artwork":
        art = work.artwork if isinstance(work.artwork, list) else []
        return len(art) > 0
    return False


def _gaps(work: Work) -> list[str]:
    return [name for name in TRACK_FIELDS if not _field_present(work, name)]


def _work_year(work: Work) -> int | None:
    if work.release_date is None:
        return None
    return int(work.release_date.year)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--sqlite", type=Path, default=None, help="r18_dump.db path")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--offset", type=int, default=0)
    parser.add_argument("--codes", default="", help="Comma-separated codes to force")
    parser.add_argument("--year", type=int, default=None, help="Prefer works with this release year")
    parser.add_argument("--only-gaps", action="store_true", default=True)
    parser.add_argument("--include-complete", action="store_true", help="Also touch works with no gaps")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--commit-every", type=int, default=50)
    parser.add_argument("--update-actress-aliases", action="store_true")
    arguments = parser.parse_args()

    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    sqlite_path = arguments.sqlite or (settings.data_dir / "r18-dumps" / "r18_dump.db")
    if not sqlite_path.is_file():
        print(f"r18 dump sqlite missing: {sqlite_path}", file=sys.stderr)
        print("Run scripts/import_r18_dump.py first.", file=sys.stderr)
        return 2

    database = Database(settings.database_url)
    database.initialize()
    filled = Counter()
    matched = 0
    updated = 0
    skipped = 0
    forced_codes = {part.strip().upper() for part in arguments.codes.split(",") if part.strip()}

    with database.session() as session, R18DumpStore(sqlite_path) as store:
        repo = Repository(session)
        works = [
            work
            for work in repo.list_works()
            if work.family == ContentFamily.JAV.value
        ]
        if forced_codes:
            selected = []
            for work in works:
                code = (work.primary_code or "").upper()
                if code in forced_codes:
                    selected.append(work)
            works = selected
        else:
            if not arguments.include_complete:
                works = [work for work in works if _gaps(work)]
            if arguments.year is not None:
                year_hits = [work for work in works if _work_year(work) == arguments.year]
                year_unknown = [work for work in works if _work_year(work) is None]
                # Prefer exact year, then undated (often the 2021 gap set), then rest.
                rest = [work for work in works if work not in year_hits and work not in year_unknown]
                works = year_hits + year_unknown + rest
            if arguments.offset:
                works = works[arguments.offset :]
            if arguments.limit:
                works = works[: arguments.limit]

        print(f"Candidates: {len(works)} (dump={sqlite_path})")
        pending_commit = 0
        for index, work in enumerate(works, start=1):
            raw_code = work.primary_code or ""
            code, family = extract_code(raw_code)
            if code is None or family is not ContentFamily.JAV:
                skipped += 1
                continue
            before = set(_gaps(work))
            record = store.build_record(code)
            if record is None:
                skipped += 1
                continue
            matched += 1
            if arguments.dry_run:
                print(f"[dry-run] {code} gaps={sorted(before)}")
                continue
            repo.merge_provider_into_work(work, record, overwrite=False)
            # Prefer Japanese plot as original_plot when empty.
            if not (work.original_plot or "").strip() and record.plot and record.language == "ja":
                work.original_plot = record.plot
                sources = dict(work.field_sources or {})
                sources.setdefault("original_plot", record.provider)
                work.field_sources = sources
            after = set(_gaps(work))
            newly = before - after
            if newly:
                updated += 1
                for name in newly:
                    filled[name] += 1
            pending_commit += 1
            if pending_commit >= max(1, arguments.commit_every):
                session.commit()
                pending_commit = 0
            if index % 100 == 0:
                print(f"  … {index}/{len(works)} matched={matched} updated={updated}")

        if arguments.update_actress_aliases and not arguments.dry_run:
            alias_path = settings.data_dir / "r18-dumps" / "actress_aliases_from_dump.json"
            existing: dict[str, str] = {}
            if alias_path.is_file():
                existing = json.loads(alias_path.read_text(encoding="utf-8"))
            added = 0
            for alias, kanji in store.iter_actress_alias_pairs():
                key = normalize_actress_key(alias)
                if not key or key in existing:
                    continue
                existing[key] = kanji
                added += 1
            alias_path.write_text(
                json.dumps(existing, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )
            print(f"Actress aliases written: +{added} → {alias_path} (total {len(existing)})")

        if pending_commit and not arguments.dry_run:
            session.commit()

    print(
        json.dumps(
            {
                "candidates": len(works),
                "matched": matched,
                "updated": updated,
                "skipped": skipped,
                "filled": dict(filled),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
