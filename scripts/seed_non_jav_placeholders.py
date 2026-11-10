"""Attach curated non-JAV actors and real public portraits only (no placeholders)."""

from __future__ import annotations

import json
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
from actor_avatars import (
    detect_image_ext,
    fetch_real_portrait,
    image_filename,
    is_designed_identicon,
    is_solid_placeholder,
    notes_indicate_placeholder,
    notes_indicate_real_photo,
    theporndb_performer_photo,
    theporndb_token_from_env,
)
from actor_avatars import _looks_searchable_person_name

from shadow_mdc.reference_models import catalog_rules  # noqa: E402
from shadow_mdc.runtime_data import data_directories  # noqa: E402


CATALOG_PATH = data_directories()[0] / "non-jav-actors.json"
IMAGE_DIR = data_directories()[0] / "actor-images"

EXTRA_ACTORS = catalog_rules().extra_actors

CHINA_EXTRA = catalog_rules().china_extra

EXTRA_ACTORS = EXTRA_ACTORS + CHINA_EXTRA


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _safe_match_names(name: str, aliases: list[str]) -> list[str]:
    names: list[str] = []
    for value in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
        key = _normalize(cleaned).lstrip("@")
        if not key:
            continue
        if key.isascii() and len("".join(ch for ch in key if ch.isalnum())) < 4:
            continue
        if not key.isascii() and len(key) < 2:
            continue
        if cleaned not in names:
            names.append(cleaned)
    return names



def _clear_actor_images(image_dir: Path, name: str) -> int:
    removed = 0
    stem = image_filename(name, ".png").rsplit(".", 1)[0]
    for leftover in image_dir.glob(f"{stem}.*"):
        leftover.unlink(missing_ok=True)
        removed += 1
    return removed


def _should_keep_existing(path: Path, notes: str | None) -> bool:
    if not path.is_file():
        return False
    if notes_indicate_placeholder(notes):
        return False
    if is_solid_placeholder(path) or is_designed_identicon(path):
        return False
    if notes_indicate_real_photo(notes):
        return True
    if path.suffix.lower() in {".jpg", ".jpeg", ".webp", ".gif"}:
        return path.stat().st_size >= 2000
    return path.stat().st_size >= 20_000 and not is_designed_identicon(path)


def main() -> None:
    catalog = json.loads(CATALOG_PATH.read_text(encoding="utf-8-sig"))
    actors: list[dict[str, object]] = list(catalog["actors"])
    existing = {_normalize(str(actor["name"])) for actor in actors}

    extra_alias_map = catalog_rules().extra_aliases

    added = 0
    for entry in EXTRA_ACTORS:
        extra = entry.model_dump()
        if _normalize(str(extra["name"])) in existing:
            continue
        aliases = list(extra.get("aliases", []))  # type: ignore[arg-type]
        name = str(extra["name"])
        actor = {
            "name": name,
            "aliases": aliases,
            "groups": list(extra["groups"]),  # type: ignore[arg-type]
            "categories": list(extra["categories"]),  # type: ignore[arg-type]
            "match_names": _safe_match_names(name, aliases),
            "image_file": None,
            "biography": extra.get("biography") or f"Curated {extra['categories'][0]} adult performer seed.",
            "notes": "Awaiting real portrait; upload via actor library or re-run portrait seed.",
        }
        actors.append(actor)
        existing.add(_normalize(name))
        added += 1

    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    token = theporndb_token_from_env(ROOT / ".env")
    real_photos = 0
    kept_existing = 0
    without_image = 0
    deleted_placeholders = 0
    source_counts: dict[str, int] = {
        "theporndb": 0,
        "wikipedia": 0,
        "wikidata": 0,
        "commons": 0,
        "existing": 0,
        "other": 0,
    }

    def _count_source(note: str | None) -> None:
        text_n = (note or "").casefold()
        if "theporndb" in text_n:
            source_counts["theporndb"] += 1
        elif "wikipedia" in text_n or "wikimedia public summary" in text_n:
            source_counts["wikipedia"] += 1
        elif "wikidata" in text_n:
            source_counts["wikidata"] += 1
        elif "commons file search" in text_n:
            source_counts["commons"] += 1
        elif "existing local" in text_n:
            source_counts["existing"] += 1
        else:
            source_counts["other"] += 1

    for index, actor in enumerate(actors, start=1):
        name = str(actor["name"])
        if index == 1 or index % 25 == 0 or index == len(actors):
            print(f"progress {index}/{len(actors)} real_photos={real_photos}", flush=True)
        extra_aliases = extra_alias_map.get(_normalize(name), [])
        aliases = list(dict.fromkeys([*actor.get("aliases", []), *extra_aliases]))  # type: ignore[arg-type]
        actor["aliases"] = aliases
        actor["match_names"] = _safe_match_names(name, aliases)
        notes = str(actor.get("notes") or "") if actor.get("notes") is not None else None

        current_name = actor.get("image_file")
        current_path = IMAGE_DIR / str(current_name) if current_name else None
        if current_path is not None and _should_keep_existing(current_path, notes):
            actor["image_file"] = current_path.name
            if not notes_indicate_real_photo(notes):
                actor["notes"] = "Existing local portrait kept (non-placeholder)."
            kept_existing += 1
            real_photos += 1
            _count_source(str(actor.get("notes") or ""))
        else:
            groups = {str(item).casefold() for item in (actor.get("groups") or [])}
            skip_groups = {
                "studio",
                "tanhua",
                "91-tanhua",
                "yuepao",
                "x-tanhua",
                "tandian",
                "xingba",
                "x-xingba",
            }
            # Prefer Latin stage names / aliases (TPDB + EN wiki coverage).
            # CJK primary names are still searchable when romanized aliases exist.
            def _latin_primary(value: str) -> bool:
                letters = sum(ch.isalpha() and ch.isascii() for ch in value)
                visible = sum(not ch.isspace() for ch in value)
                return letters >= 4 and letters >= max(1, visible // 2)

            name_values = [name, *aliases]
            has_latin = any(_latin_primary(value) for value in name_values)
            searchable = (
                not (groups & skip_groups)
                and ("western" in groups or "onlyfans" in groups or has_latin)
                and any(_looks_searchable_person_name(value) for value in name_values)
            )
            fetched = None
            if searchable:
                # Full public-source pipeline for Western / OnlyFans stage names.
                # For CJK-primary catalog rows with romanized aliases, TPDB alone is
                # the realistic hit rate; skip slow wiki/commons misses.
                if "western" in groups or "onlyfans" in groups or _latin_primary(name):
                    fetched = fetch_real_portrait(name, aliases, theporndb_token=token)
                elif token:
                    photo = theporndb_performer_photo(name, aliases, token)
                    if photo is not None:
                        fetched = (photo, "Portrait from ThePornDB performer image.")
            if fetched is not None:
                content, source_note = fetched
                ext = detect_image_ext(content) or ".jpg"
                filename = image_filename(name, ext)
                deleted_placeholders += _clear_actor_images(IMAGE_DIR, name)
                (IMAGE_DIR / filename).write_bytes(content)
                actor["image_file"] = filename
                actor["notes"] = source_note
                real_photos += 1
                _count_source(source_note)
            else:
                deleted_placeholders += _clear_actor_images(IMAGE_DIR, name)
                actor["image_file"] = None
                if notes_indicate_placeholder(notes) or not notes:
                    actor["notes"] = "No public portrait found; UI shows initials until upload."
                without_image += 1

        if not actor.get("biography"):
            cats = ", ".join(actor.get("categories") or [])  # type: ignore[arg-type]
            actor["biography"] = f"Seeded non-JAV performer ({cats})."

    referenced = {str(actor.get("image_file")) for actor in actors if actor.get("image_file")}
    for path_item in IMAGE_DIR.iterdir():
        if not path_item.is_file():
            continue
        if path_item.name in referenced:
            continue
        if is_solid_placeholder(path_item) or is_designed_identicon(path_item):
            path_item.unlink(missing_ok=True)
            deleted_placeholders += 1

    actors.sort(key=lambda item: str(item["name"]).casefold())
    catalog["actors"] = actors
    catalog["source"] = "avtor.txt+real-portraits"
    CATALOG_PATH.write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(
        f"actors={len(actors)} added={added} real_photos={real_photos} "
        f"kept_existing={kept_existing} without_image={without_image} "
        f"deleted_placeholders={deleted_placeholders} theporndb={'yes' if token else 'no'} "
        f"sources={source_counts}"
    )


if __name__ == "__main__":
    main()
