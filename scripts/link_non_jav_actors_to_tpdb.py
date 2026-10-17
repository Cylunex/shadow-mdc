#!/usr/bin/env python3
"""Link non-JAV actors to their ThePornDB performer and import that performer's scenes.

Many non-JAV actor profiles came from creator/blogger imports and therefore have no
works at all: the catalog knew the name, but nothing connected it to a scene. ThePornDB
knows most of those performers, so this script:

1. finds the performer by exact name / alias match (no fuzzy guessing),
2. fetches their newest scenes (``/performers/{id}/scenes``),
3. upserts those scenes as works (provider ``theporndb``) with the performer in
   ``work.actors`` — which is what makes them appear on the actor page,
4. downloads the scene poster into ``data/artwork/<work>/poster.*`` and the performer
   portrait into ``data/actor-images/``,
5. stores the short bio / career years on the profile.

Never invents data: a missing performer or an empty scene list is skipped and
counted. Use ``--dry-run`` first; the catalog JSON is backed up before writing.

Example::

    PYTHONPATH=src .venv/bin/python scripts/link_non_jav_actors_to_tpdb.py --dry-run --limit 10
    PYTHONPATH=src .venv/bin/python scripts/link_non_jav_actors_to_tpdb.py --limit 60
"""

from __future__ import annotations

import argparse
import io
import json
import re
import shutil
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from actor_avatars import detect_image_ext, image_filename, theporndb_token_from_env  # noqa: E402
from enrich_non_jav_incomplete import scene_to_record  # noqa: E402

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.domain import ProviderRecord  # noqa: E402
from shadow_mdc.enums import ContentFamily, MediaCategory  # noqa: E402
from shadow_mdc.media import directory_index  # noqa: E402
from shadow_mdc.services.non_jav_actor_catalog import (  # noqa: E402
    NonJavActorCatalog,
    NonJavActorCatalogStore,
    NonJavActorProfile,
)
from shadow_mdc.services.studio_guard import classify_non_jav_actor_rejection  # noqa: E402

API = "https://api.theporndb.net"
UA = "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc)"
MIN_IMAGE_BYTES = 1500


def _normalize(value: str) -> str:
    return re.sub(r"\s+", "", unicodedata.normalize("NFKC", value or "").casefold())


_CHINESE_SITE_MARKERS = (
    "swag",
    "madou",
    "modelmediaasia",
    "tianmei",
    "xingkong",
    "jelly",
    "peachmedia",
    "tangxin",
)


def _family_for_site(
    site: str | None, fallback: tuple[ContentFamily, MediaCategory]
) -> tuple[ContentFamily, MediaCategory]:
    """The scene's site decides the content family, not the profile's stale import groups."""

    name = (site or "").casefold()
    if any(marker in name for marker in _CHINESE_SITE_MARKERS):
        return ContentFamily.CHINESE, MediaCategory.CHINA
    if name:
        return ContentFamily.WESTERN, MediaCategory.EUROPE
    return fallback


def _family_for(profile: NonJavActorProfile) -> tuple[ContentFamily, MediaCategory]:
    groups = {group.casefold() for group in profile.groups}
    if "korean" in groups:
        return ContentFamily.KOREAN, MediaCategory.KOREA
    if groups & {"chinese", "madou", "swag", "tianmei", "xingkong", "tangxin", "tanhua"}:
        return ContentFamily.CHINESE, MediaCategory.CHINA
    return ContentFamily.WESTERN, MediaCategory.EUROPE


@dataclass
class Stats:
    actors_seen: int = 0
    matched_performers: int = 0
    works_created: int = 0
    works_updated: int = 0
    posters: int = 0
    portraits: int = 0
    no_performer: list[str] = field(default_factory=list)
    no_scenes: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


def performer_candidates(client: httpx.Client, profile: NonJavActorProfile) -> list[dict[str, Any]]:
    keys = {_normalize(profile.name), *(_normalize(alias) for alias in profile.aliases)}
    keys.discard("")
    found: dict[str, dict[str, Any]] = {}
    for query in [profile.name, *profile.aliases][:3]:
        query = (query or "").strip()
        if not query:
            continue
        try:
            response = client.get(f"{API}/performers", params={"q": query, "per_page": 8})
        except httpx.HTTPError:
            time.sleep(1.0)
            continue
        if response.status_code != 200:
            continue
        for row in response.json().get("data") or []:
            if not isinstance(row, dict) or not row.get("id"):
                continue
            names = {row.get("name"), row.get("full_name"), *(row.get("aliases") or [])}
            if keys & {_normalize(str(name)) for name in names if name}:
                found[str(row["id"])] = row
        if found:
            break
        time.sleep(0.15)
    return list(found.values())


def performer_scenes(client: httpx.Client, performer_id: str, limit: int) -> list[dict[str, Any]]:
    try:
        response = client.get(
            f"{API}/performers/{performer_id}/scenes",
            params={"per_page": max(1, limit)},
        )
    except httpx.HTTPError:
        return []
    if response.status_code != 200:
        return []
    rows = response.json().get("data") or []
    return [row for row in rows if isinstance(row, dict) and row.get("id")]


def _fetch_bytes(client: httpx.Client, url: str | None) -> bytes | None:
    if not url:
        return None
    try:
        response = client.get(url, headers={"User-Agent": UA})
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    if len(response.content) < MIN_IMAGE_BYTES:
        return None
    return response.content


def _as_jpeg(content: bytes) -> bytes | None:
    if detect_image_ext(content) is not None:
        return content
    try:
        from PIL import Image
    except ImportError:
        return None
    try:
        with Image.open(io.BytesIO(content)) as image:
            buffer = io.BytesIO()
            image.convert("RGB").save(buffer, format="JPEG", quality=88, optimize=True)
            return buffer.getvalue()
    except Exception:
        return None


def save_work_poster(artwork_dir: Path, work_id: str, content: bytes, source: str) -> bool:
    payload = _as_jpeg(content)
    if payload is None:
        return False
    suffix = detect_image_ext(payload) or ".jpg"
    target_dir = artwork_dir / work_id
    target_dir.mkdir(parents=True, exist_ok=True)
    target = target_dir / f"poster{suffix}"
    temporary = target.with_suffix(f"{target.suffix}.part")
    temporary.write_bytes(payload)
    temporary.replace(target)
    (target_dir / "source.txt").write_text(source + "\n", encoding="utf-8")
    directory_index.invalidate(target_dir)
    return True


def save_actor_portrait(image_dir: Path, name: str, content: bytes) -> str | None:
    payload = _as_jpeg(content)
    if payload is None:
        return None
    suffix = detect_image_ext(payload) or ".jpg"
    filename = image_filename(name, suffix)
    image_dir.mkdir(parents=True, exist_ok=True)
    target = image_dir / filename
    temporary = target.with_suffix(f"{target.suffix}.part")
    temporary.write_bytes(payload)
    temporary.replace(target)
    directory_index.invalidate(image_dir)
    return filename


def scene_poster_url(scene: dict[str, Any]) -> str | None:
    posters = scene.get("posters")
    if isinstance(posters, dict):
        for key in ("full", "large", "medium", "small"):
            value = posters.get(key)
            if isinstance(value, str) and value.startswith("http"):
                return value
    for key in ("poster_image", "poster", "image", "background"):
        value = scene.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def performer_portrait_url(performer: dict[str, Any]) -> str | None:
    for key in ("image", "face", "thumbnail"):
        value = performer.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    posters = performer.get("posters")
    if isinstance(posters, list):
        for item in posters:
            if isinstance(item, dict):
                url = item.get("url")
                if isinstance(url, str) and url.startswith("http"):
                    return url
    return None


def performer_bio(performer: dict[str, Any]) -> str | None:
    bio = performer.get("bio")
    if isinstance(bio, str) and bio.strip():
        text = " ".join(bio.split())
        if len(text) > 320:
            cut = text[:320].rsplit(" ", 1)[0]
            return f"{cut or text[:320]}…"
        return text
    extras = performer.get("extras") if isinstance(performer.get("extras"), dict) else {}
    parts: list[str] = []
    for key, label in (("ethnicity", "族裔"), ("birthplace", "出生地"), ("birthday", "生日")):
        value = extras.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(f"{label} {value.strip()}")
    for key, label in (("career_start_year", "出道"), ("career_end_year", "退役")):
        value = extras.get(key)
        if isinstance(value, int) and value > 1900:
            parts.append(f"{label} {value}")
    return " · ".join(parts) if parts else None


def _linked_scene_count(profile: NonJavActorProfile) -> int:
    match = re.search(r"linked via (\d+) scenes", profile.notes or "")
    return int(match.group(1)) if match else 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="max actors to probe")
    parser.add_argument("--scenes-per-actor", type=int, default=8)
    parser.add_argument("--name", action="append", default=None, help="only these actor names")
    parser.add_argument("--all", action="store_true", help="include actors that already have works")
    parser.add_argument("--refresh-images", action="store_true", help="re-download portraits that exist")
    parser.add_argument(
        "--profiles-only",
        action="store_true",
        help="refresh already-linked profiles (categories / bio / portrait), import no scenes",
    )
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
    data_dir = settings.data_dir
    artwork_dir = data_dir / "artwork"
    image_dir = data_dir / "actor-images"

    token = arguments.token or theporndb_token_from_env(ROOT / ".env") or settings.theporndb_token
    if not token:
        print("SHADOW_MDC_THEPORNDB_TOKEN (or TPDB_TOKEN) missing", file=sys.stderr)
        return 2

    store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    catalog = store.load()
    database = Database(settings.database_url)
    database.initialize()

    work_actor_names: set[str] = set()
    works_categories: dict[str, Counter[str]] = {}
    with database.session() as session:
        repo = Repository(session)
        for work in repo.list_works():
            if work.family == ContentFamily.JAV.value:
                continue
            for name in work.actors or []:
                if not isinstance(name, str):
                    continue
                key = _normalize(name)
                work_actor_names.add(key)
                works_categories.setdefault(key, Counter())[str(work.category)] += 1

    wanted = {_normalize(name) for name in (arguments.name or [])}
    def _already_linked(actor: NonJavActorProfile) -> bool:
        return "theporndb performer" in (actor.notes or "").casefold()

    targets = [
        actor
        for actor in catalog.actors
        if (not wanted or _normalize(actor.name) in wanted)
        and (
            _already_linked(actor)
            if arguments.profiles_only
            else arguments.all or _normalize(actor.name) not in work_actor_names
        )
        and classify_non_jav_actor_rejection(actor.name) is None
    ]
    targets.sort(key=lambda actor: actor.name.casefold())
    if arguments.limit is not None:
        targets = targets[: max(1, arguments.limit)]
    print(f"actors without works: {len(targets)} / {len(catalog.actors)}")

    stats = Stats()
    updated_profiles: dict[str, NonJavActorProfile] = {}
    created_records: list[tuple[ProviderRecord, dict[str, Any], NonJavActorProfile]] = []

    with httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        proxy=settings.proxy_url,
        headers={"User-Agent": UA, "Accept": "application/json"},
    ) as anonymous, httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        proxy=settings.proxy_url,
        headers={"User-Agent": UA, "Accept": "application/json", "Authorization": f"Bearer {token}"},
    ) as client:
        for profile in targets:
            stats.actors_seen += 1
            performers = performer_candidates(client, profile)
            if not performers:
                stats.no_performer.append(profile.name)
                print(f"[no-performer] {profile.name}")
                continue
            performer = performers[0]
            stats.matched_performers += 1
            scenes = (
                []
                if arguments.profiles_only
                else performer_scenes(client, str(performer["id"]), arguments.scenes_per_actor)
            )
            if not scenes and not arguments.profiles_only:
                stats.no_scenes.append(profile.name)
                print(f"[no-scenes] {profile.name} (performer id {performer['id']})")
                continue
            first_site = None
            if scenes:
                site_row = scenes[0].get("site")
                if isinstance(site_row, dict):
                    first_site = str(site_row.get("short_name") or site_row.get("name") or "") or None
            family, category = _family_for_site(first_site, _family_for(profile))
            dominant = works_categories.get(_normalize(profile.name))
            if dominant:
                best = dominant.most_common(1)[0][0]
                if best in MediaCategory._value2member_map_:
                    category = MediaCategory(best)
                    family = {
                        MediaCategory.CHINA: ContentFamily.CHINESE,
                        MediaCategory.KOREA: ContentFamily.KOREAN,
                        MediaCategory.EUROPE: ContentFamily.WESTERN,
                    }.get(category, family)
            print(
                f"[match] {profile.name} -> performer {performer.get('name')} · "
                f"{'profile refresh' if arguments.profiles_only else f'{len(scenes)} scenes'}"
            )

            portrait_name = None
            portrait = (
                None
                if profile.image_file and not arguments.refresh_images
                else _fetch_bytes(client, performer_portrait_url(performer))
            )
            if portrait is not None:
                if arguments.dry_run:
                    portrait_name = "<dry-run>"
                else:
                    portrait_name = save_actor_portrait(image_dir, profile.name, portrait)
                if portrait_name:
                    stats.portraits += 1

            for scene in scenes:
                record = scene_to_record(scene, family=family, category=category)
                actors = list(record.actors)
                if not any(_normalize(name) == _normalize(profile.name) for name in actors):
                    actors = [profile.name, *actors]
                record = record.model_copy(update={"actors": tuple(actors)})
                created_records.append((record, scene, profile))

            bio = performer_bio(performer)
            scene_count = len(scenes) if scenes else _linked_scene_count(profile)
            notes = f"ThePornDB performer {performer['id']} · linked via {scene_count} scenes"
            updates_map: dict[str, object] = {"notes": notes}
            # The seeded/imported category was a guess; matched scenes are evidence.
            if tuple(profile.categories) != (category.value,):
                updates_map["categories"] = (category.value,)
            seeded_bio = "seeded non-jav performer" in (profile.biography or "").casefold()
            if bio and (not profile.biography or seeded_bio):
                updates_map["biography"] = bio
            elif seeded_bio:
                updates_map["biography"] = None
            if portrait_name and portrait_name != "<dry-run>":
                updates_map["image_file"] = portrait_name
            updated_profiles[profile.name] = profile.model_copy(update=updates_map)
            time.sleep(0.2)

        if created_records:
            with database.session() as session:
                repo = Repository(session)
                for record, scene, _profile in created_records:
                    existing = repo.find_work_by_provider_identity(record.provider, record.external_id)
                    if existing is not None:
                        stats.works_updated += 1
                        if arguments.dry_run:
                            continue
                        if record.actors:
                            repo.update_work_fields(existing, actors=list(record.actors), lock_edited=False)
                        continue
                    stats.works_created += 1
                    if arguments.dry_run:
                        continue
                    work = repo.upsert_provider_record(record, overwrite=False)
                    try:
                        content = _fetch_bytes(anonymous, scene_poster_url(scene))
                    except Exception as exc:
                        stats.errors.append(f"{record.external_id}: {type(exc).__name__}")
                        content = None
                    if content is not None and save_work_poster(
                        artwork_dir, work.id, content, f"theporndb:{record.external_id}"
                    ):
                        stats.posters += 1

    if updated_profiles and not arguments.dry_run:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        shutil.copy2(data_dir / "non-jav-actors.json", data_dir / f"non-jav-actors.json.bak-tpdb-{stamp}")
        actors = [updated_profiles.get(actor.name, actor) for actor in catalog.actors]
        store.save(
            NonJavActorCatalog.model_validate(
                {"version": catalog.version, "source": catalog.source, "actors": actors}
            )
        )

    print(
        json.dumps(
            {
                "actors_seen": stats.actors_seen,
                "matched_performers": stats.matched_performers,
                "works_created": stats.works_created,
                "works_updated": stats.works_updated,
                "posters": stats.posters,
                "portraits": stats.portraits,
                "no_performer": len(stats.no_performer),
                "no_scenes": len(stats.no_scenes),
                "dry_run": arguments.dry_run,
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
