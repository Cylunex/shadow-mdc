#!/usr/bin/env python3
"""Add missing real non-JAV performers from works into non-jav-actors.json.

Skips studio/label/探花 channel names via studio_guard. Optionally downloads
real portraits (ThePornDB → Wikipedia/Wikidata) — never invents placeholders.

Example::

    PYTHONPATH=src .venv/bin/python scripts/sync_non_jav_actors_from_works.py --dry-run
    PYTHONPATH=src .venv/bin/python scripts/sync_non_jav_actors_from_works.py --fetch-portraits --limit 40
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from actor_avatars import (  # noqa: E402
    detect_image_ext,
    fetch_real_portrait,
    image_filename,
    theporndb_token_from_env,
)
from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.enums import ContentFamily, MediaCategory  # noqa: E402
from shadow_mdc.services.non_jav_actor_catalog import (  # noqa: E402
    NonJavActorCatalogStore,
    build_non_jav_actor_profile,
)
from shadow_mdc.services.studio_guard import classify_non_jav_actor_rejection  # noqa: E402


def _family_category(family: str | None) -> MediaCategory:
    mapping = {
        ContentFamily.CHINESE.value: MediaCategory.CHINA,
        ContentFamily.WESTERN.value: MediaCategory.EUROPE,
        ContentFamily.KOREAN.value: MediaCategory.KOREA,
    }
    return mapping.get(family or "", MediaCategory.EUROPE)


def _groups_for(family: str | None, studio: str | None) -> list[str]:
    groups: list[str] = []
    studio_l = (studio or "").casefold()
    if family == ContentFamily.CHINESE.value:
        groups.append("chinese")
    elif family == ContentFamily.WESTERN.value:
        groups.append("western")
    elif family == ContentFamily.KOREAN.value:
        groups.append("korean")
    if "madou" in studio_l or "麻豆" in (studio or ""):
        groups.append("madou")
    if "swag" in studio_l:
        groups.append("swag")
    if "model media" in studio_l:
        groups.append("model-media")
    return groups or ["non-jav"]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--fetch-portraits", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="max new actors to add")
    parser.add_argument("--token", default=None, help="TPDB token for portraits")
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

    store = NonJavActorCatalogStore(settings.data_dir / "non-jav-actors.json")
    catalog = store.load()
    known: set[str] = set()
    for actor in catalog.actors:
        known.add(actor.name.casefold())
        for alias in actor.aliases:
            known.add(alias.casefold())
        for match in actor.match_names:
            known.add(match.casefold())

    database = Database(settings.database_url)
    database.initialize()

    # name -> (family, studio) first-seen
    candidates: dict[str, tuple[str | None, str | None]] = {}
    with database.session() as session:
        for work in Repository(session).list_works():
            if work.family == ContentFamily.JAV.value:
                continue
            for name in work.actors or []:
                if not isinstance(name, str):
                    continue
                cleaned = name.strip()
                if not cleaned or cleaned.casefold() in known:
                    continue
                if classify_non_jav_actor_rejection(cleaned) is not None:
                    continue
                if cleaned.casefold() in {k.casefold() for k in candidates}:
                    continue
                candidates[cleaned] = (work.family, work.studio)

    ordered = sorted(candidates.items(), key=lambda item: item[0].casefold())
    if arguments.limit is not None:
        ordered = ordered[: max(1, arguments.limit)]

    token = arguments.token or theporndb_token_from_env(ROOT / ".env") or settings.theporndb_token
    image_dir = settings.data_dir / "actor-images"
    image_dir.mkdir(parents=True, exist_ok=True)

    added = 0
    portraits = 0
    skipped_no_photo = 0
    new_actors = list(catalog.actors)

    for name, (family, studio) in ordered:
        image_file = None
        notes = "Synced from non-JAV works catalog."
        if arguments.fetch_portraits:
            photo = fetch_real_portrait(name, [], theporndb_token=token)
            if photo is None:
                skipped_no_photo += 1
                print(f"[skip-photo] {name!r} (still adding without image)" if not arguments.dry_run else f"[dry] {name!r} no portrait yet")
            else:
                content, note = photo
                ext = detect_image_ext(content) or ".jpg"
                filename = image_filename(name, ext)
                if not arguments.dry_run:
                    (image_dir / filename).write_bytes(content)
                image_file = filename
                notes = note
                portraits += 1
                print(f"[portrait] {name!r} -> {filename}")

        profile = build_non_jav_actor_profile(
            name=name,
            aliases=(),
            groups=tuple(_groups_for(family, studio)),
            categories=(_family_category(family),),
            image_file=image_file,
            biography=f"Auto-synced from works (studio={studio or '—'}).",
            notes=notes,
        )
        print(f"[add] {profile.name!r} groups={list(profile.groups)} image={image_file}")
        if not arguments.dry_run:
            new_actors.append(profile)
        added += 1

    if not arguments.dry_run and added:
        store.save(catalog.model_copy(update={"actors": tuple(new_actors)}))

    summary = {
        "candidates": len(candidates),
        "added": added,
        "portraits": portraits,
        "skipped_no_photo": skipped_no_photo,
        "dry_run": arguments.dry_run,
        "catalog_path": str(settings.data_dir / "non-jav-actors.json"),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
