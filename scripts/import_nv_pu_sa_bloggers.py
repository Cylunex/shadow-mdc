#!/usr/bin/env python3
"""Import X bloggers with identifiable adult works from nv-pu-sa.pages.dev.

Data flow:
  1. Load curated archive (local snapshot or live /data/archive.json / R2).
  2. Discover ThePornDB sites/scenes that truly match each creator
     (scene.site.short_name must equal the queried site — TPDB returns junk
     for unknown site slugs).
  3. Upsert non-JAV actors (group: blogger) with real X avatars + bios.
  4. Attach TPDB scenes as works (+ posters when available).
  5. Live-verify X handles via shadow_mdc.services.x_handle before storing.

Local data only for catalog/images. Never prints ThePornDB tokens.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import unicodedata
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(ROOT / "src"))

from actor_avatars import (  # noqa: E402
    detect_image_ext,
    image_filename,
    notes_indicate_real_photo,
    theporndb_token_from_env,
)
from shadow_mdc.services.x_handle import (  # noqa: E402
    normalize_x_handle,
    verify_x_handle_exists,
)

ACTORS_PATH = ROOT / "data" / "non-jav-actors.json"
WORKS_PATH = ROOT / "data" / "non-jav-works.json"
ARCHIVE_PATH = ROOT / "data" / "nv-pu-sa-archive.json"
IMAGE_DIR = ROOT / "data" / "actor-images"
ARTWORK_DIR = ROOT / "data" / "artwork"

ARCHIVE_URLS = (
    "https://nv-pu-sa.pages.dev/data/archive.json",
    "https://img.boomboomboom.ggff.net/data/archive.json",
)

_USER_AGENT = "ShadowMDC/0.1 (https://github.com/Cylunex/shadow-mdc; nv-pu-sa-import)"

_ADULT_BIO = re.compile(
    r"onlyfans|fansly|fansone|swag|linktr|作品|订阅|糖心|nsfw|of\.|"
    r"patreon|telegram|vip|福利|成人|完整版|麻豆|iyaofans|myfans|fanvue",
    re.I,
)

# Known TPDB site short_names that do not follow fansdb{handle}onlyfans.
_KNOWN_SITES: dict[str, tuple[str, ...]] = {
    "Anaimiya": ("fansdbanaimiyaonlyfans", "hobbypornnaiminaimi"),
    "HongKong_Doll": (
        "fansdbhongkongdollonlyfans",
        "fansdbhongkongdollfansly",
        "hobbypornhongkongdoll",
    ),
    "SpicygumL": ("spicygum",),
    "Nana_taipei": ("fansdbnanataipeionlyfans",),
    "yui_xin_tw": ("fansdbyuixintwonlyfans",),
}

# Prefer these display names when the X name is emoji-heavy.
_PREFERRED_NAMES: dict[str, str] = {
    "Anaimiya": "Naimi奶咪",
    "waifupupu": "pupuwaifu",
    "HongKong_Doll": "HongKongDoll",
    "SpicygumL": "June Liu",
    "FortuneCutie00": "FortuneCutie饼干姐姐",
    "Nana_taipei": "Nana Taipei",
    "yui_xin_tw": "辛尤里",
    "kunkunkun188": "困困狗",
}


def collapse_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", unicodedata.normalize("NFKC", value).casefold())


def bare_handle(screen_name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", screen_name.casefold())


def clean_display_name(name: str, screen_name: str) -> str:
    preferred = _PREFERRED_NAMES.get(screen_name)
    if preferred:
        return preferred
    normalized = unicodedata.normalize("NFKC", name or "")
    # Drop most emoji / symbols; keep CJK + alnum + spaces + common separators.
    cleaned = re.sub(r"[^\w\u4e00-\u9fff\s·・._-]+", " ", normalized, flags=re.UNICODE)
    cleaned = " ".join(cleaned.split()).strip(" ._-|")
    if len(cleaned) >= 2:
        return cleaned
    return screen_name


def site_guesses(screen_name: str) -> list[str]:
    bare = bare_handle(screen_name)
    guesses = [
        f"fansdb{bare}onlyfans",
        f"fansdb{bare}fansly",
        f"fansdb{bare}manyvids",
        f"hobbyporn{bare}",
        f"manyvids{bare}",
    ]
    bare2 = re.sub(r"\d+$", "", bare)
    if bare2 and bare2 != bare and len(bare2) >= 4:
        guesses.extend([f"fansdb{bare2}onlyfans", f"hobbyporn{bare2}"])
    guesses.extend(_KNOWN_SITES.get(screen_name, ()))
    return list(dict.fromkeys(guesses))


def load_archive(*, refresh: bool) -> list[dict[str, Any]]:
    if refresh or not ARCHIVE_PATH.is_file():
        with httpx.Client(timeout=60.0, follow_redirects=True, headers={"User-Agent": _USER_AGENT}) as client:
            last_error: Exception | None = None
            for url in ARCHIVE_URLS:
                try:
                    response = client.get(url)
                    response.raise_for_status()
                    data = response.json()
                    if isinstance(data, list) and data:
                        ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
                        ARCHIVE_PATH.write_text(
                            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
                            encoding="utf-8",
                        )
                        print(f"archive fetched {len(data)} from {url}", flush=True)
                        return data
                except (httpx.HTTPError, ValueError) as exc:
                    last_error = exc
            raise SystemExit(f"failed to fetch nv-pu-sa archive: {last_error}")
    data = json.loads(ARCHIVE_PATH.read_text(encoding="utf-8"))
    if not isinstance(data, list):
        raise SystemExit("nv-pu-sa archive must be a JSON list")
    print(f"archive loaded {len(data)} from {ARCHIVE_PATH}", flush=True)
    return data


def adult_candidates(archive: list[dict[str, Any]], *, min_followers: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for row in archive:
        sn = str(row.get("screen_name") or "").strip()
        if not sn or row.get("is_blocked") or row.get("is_suspended"):
            continue
        name = str(row.get("name") or "")
        bio = str(row.get("description") or "")
        followers = int(row.get("followers_count") or 0)
        if _ADULT_BIO.search(bio) or _ADULT_BIO.search(name) or followers >= min_followers:
            out.append(row)
    out.sort(key=lambda item: int(item.get("followers_count") or 0), reverse=True)
    return out


def fetch_site_scenes(
    client: httpx.Client, short_name: str, *, pages: int, per_page: int = 50
) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            response = client.get(
                "https://api.theporndb.net/scenes",
                params={"site": short_name, "per_page": per_page, "page": page},
            )
        except httpx.HTTPError:
            break
        if response.status_code != 200:
            break
        try:
            batch = response.json().get("data") or []
        except ValueError:
            break
        if not isinstance(batch, list) or not batch:
            break
        for item in batch:
            if not isinstance(item, dict):
                continue
            site = item.get("site") or {}
            actual = ""
            if isinstance(site, dict):
                actual = str(site.get("short_name") or "")
            # Critical: TPDB returns unrelated popular scenes for unknown sites.
            if actual.casefold() != short_name.casefold():
                continue
            scenes.append(item)
        if len(batch) < per_page:
            break
    return scenes


def discover_real_sites(
    client: httpx.Client, screen_name: str, *, probe_pages: int = 1
) -> list[str]:
    found: list[str] = []
    for short in site_guesses(screen_name):
        scenes = fetch_site_scenes(client, short, pages=probe_pages, per_page=5)
        if scenes:
            found.append(short)
    return found


def scene_q_hits(
    client: httpx.Client, screen_name: str, display_name: str
) -> list[dict[str, Any]]:
    queries = [screen_name, display_name, re.sub(r"_+", " ", screen_name)]
    hits: list[dict[str, Any]] = []
    bare = bare_handle(screen_name)
    name_tokens = [
        tok
        for tok in re.findall(r"[\w\u4e00-\u9fff]{2,}", display_name)
        if tok.casefold() not in {"the", "and", "for"}
    ]
    for query in queries:
        if not query or len(query) < 3:
            continue
        try:
            response = client.get(
                "https://api.theporndb.net/scenes",
                params={"q": query, "per_page": 20},
            )
        except httpx.HTTPError:
            continue
        if response.status_code != 200:
            continue
        try:
            rows = response.json().get("data") or []
        except ValueError:
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            site = row.get("site") or {}
            short = str(site.get("short_name") or "") if isinstance(site, dict) else ""
            sname = str(site.get("name") or "") if isinstance(site, dict) else str(site or "")
            title = str(row.get("title") or "")
            blob = f"{short} {sname} {title}".casefold()
            if bare and bare in re.sub(r"[^a-z0-9]", "", short):
                hits.append(row)
                continue
            if screen_name.casefold() in sname.casefold().replace(" ", ""):
                hits.append(row)
                continue
            if any(tok.casefold() in blob for tok in name_tokens if len(tok) >= 3):
                # Require FansDB/HobbyPorn/SpicyGum-ish host to reduce false positives.
                if any(
                    marker in short.casefold() or marker in sname.casefold()
                    for marker in ("fansdb", "hobbyporn", "spicygum", "swaglive", "onlyfans")
                ):
                    hits.append(row)
        if hits:
            break
    return hits


R2_CDN_BASE = "https://img.boomboomboom.ggff.net"
SITE_ORIGIN = "https://nv-pu-sa.pages.dev"


def resolve_media_url(url: str) -> list[str]:
    """Expand gallery-relative /api/media paths into fetch candidates."""
    if not isinstance(url, str) or not url.strip():
        return []
    raw = url.strip()
    candidates: list[str] = []
    if raw.startswith("/api/media"):
        candidates.append(f"{SITE_ORIGIN}{raw}")
        from urllib.parse import parse_qs, urlparse

        key = (parse_qs(urlparse(raw).query).get("key") or [None])[0]
        if key:
            candidates.append(f"{R2_CDN_BASE}/{key.lstrip('/')}")
        media_url = (parse_qs(urlparse(raw).query).get("url") or [None])[0]
        if media_url:
            candidates.append(media_url)
    elif raw.startswith("http://") or raw.startswith("https://"):
        candidates.append(raw)
        if "pbs.twimg.com" in raw:
            candidates.append(re.sub(r"_(normal|bigger|mini)\.", "_400x400.", raw))
            from urllib.parse import quote

            candidates.append(f"{SITE_ORIGIN}/api/media?url={quote(raw, safe='')}")
    else:
        candidates.append(f"{SITE_ORIGIN}/{raw.lstrip('/')}")
    return list(dict.fromkeys(candidates))


def download_image(client: httpx.Client, url: str) -> bytes | None:
    # Prefer a clean request: drop Authorization so Twitter/R2 CDNs are not confused.
    for candidate in resolve_media_url(url):
        try:
            response = client.get(
                candidate,
                headers={"User-Agent": _USER_AGENT, "Authorization": None},
            )
            response.raise_for_status()
        except httpx.HTTPError:
            continue
        content = response.content
        if detect_image_ext(content) is None or len(content) < 4000:
            continue
        return content
    return None


def save_actor_image(name: str, content: bytes) -> str:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ext = detect_image_ext(content) or ".jpg"
    filename = image_filename(name, ext)
    digest = hashlib_sha(name)
    for path in IMAGE_DIR.glob(f"{digest}.*"):
        if path.name != filename:
            path.unlink(missing_ok=True)
    (IMAGE_DIR / filename).write_bytes(content)
    return filename


def hashlib_sha(name: str) -> str:
    import hashlib

    return hashlib.sha256(unicodedata.normalize("NFKC", name).casefold().strip().encode("utf-8")).hexdigest()


def scene_poster_url(scene: dict[str, Any]) -> str | None:
    for key in ("poster", "poster_image", "image", "background"):
        value = scene.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
        if isinstance(value, dict):
            for nested in ("full", "large", "url", "medium"):
                candidate = value.get(nested)
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    return candidate
    return None


def write_work_poster(work_id: str, content: bytes, source: str) -> Path:
    target_dir = ARTWORK_DIR / work_id
    target_dir.mkdir(parents=True, exist_ok=True)
    for path in target_dir.glob("poster.*"):
        if path.stat().st_size < 2500:
            path.unlink(missing_ok=True)
    ext = detect_image_ext(content) or ".jpg"
    target = target_dir / f"poster{ext}"
    target.write_bytes(content)
    (target_dir / "source.txt").write_text(source + "\n", encoding="utf-8")
    return target


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", value).strip("-").casefold()
    return slug or "scene"


def year_from_scene(scene: dict[str, Any]) -> int | None:
    for key in ("date", "release_date", "created"):
        value = scene.get(key)
        if isinstance(value, str) and len(value) >= 4 and value[:4].isdigit():
            year = int(value[:4])
            if 1990 <= year <= 2100:
                return year
    return None


def build_actor_index(actors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for actor in actors:
        keys = {collapse_key(str(actor.get("name") or ""))}
        for alias in actor.get("aliases") or []:
            keys.add(collapse_key(str(alias)))
        for match in actor.get("match_names") or []:
            keys.add(collapse_key(str(match)))
        for key in keys:
            if key:
                index[key] = actor
    return index


def safe_match_names(name: str, aliases: list[str]) -> list[str]:
    out: list[str] = []
    for value in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
        if not cleaned:
            continue
        key = collapse_key(cleaned)
        if not key:
            continue
        if key.isascii() and len(re.sub(r"[^a-z0-9]", "", key)) < 4:
            continue
        if not key.isascii() and len(key) < 2:
            continue
        if cleaned not in out:
            out.append(cleaned)
    return out


def upsert_blogger(
    actors: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    *,
    name: str,
    aliases: list[str],
    groups: list[str],
    biography: str | None,
    image_file: str | None,
    x_handle: str | None,
    notes: str,
) -> tuple[dict[str, Any], bool]:
    existing = None
    for candidate in (name, *aliases):
        existing = index.get(collapse_key(candidate))
        if existing is not None:
            break
    created = existing is None
    if existing is None:
        actor = {
            "name": name,
            "aliases": [a for a in dict.fromkeys(aliases) if a != name],
            "groups": list(dict.fromkeys(groups)),
            "categories": ["China", "Other"],
            "match_names": safe_match_names(name, aliases),
            "image_file": image_file,
            "x_handle": x_handle,
            "biography": biography,
            "notes": notes,
        }
        actors.append(actor)
        for key in {collapse_key(name), *[collapse_key(a) for a in aliases]}:
            if key:
                index[key] = actor
        return actor, True

    merged_aliases = list(
        dict.fromkeys([*(existing.get("aliases") or []), *aliases, name])
    )
    existing["aliases"] = [item for item in merged_aliases if item != existing["name"]]
    existing["groups"] = list(dict.fromkeys([*(existing.get("groups") or []), *groups]))
    existing["categories"] = list(
        dict.fromkeys([*(existing.get("categories") or []), "China", "Other"])
    )
    if biography and (
        not existing.get("biography")
        or existing.get("biography", "").startswith("Seeded")
        or "nv-pu-sa" in notes.casefold()
    ):
        existing["biography"] = biography
    if image_file and (
        not existing.get("image_file")
        or not notes_indicate_real_photo(str(existing.get("notes") or ""))
        or "twitter" in notes.casefold()
        or "x profile" in notes.casefold()
    ):
        # Prefer X avatar when we explicitly fetched one for this import.
        if "twitter" in notes.casefold() or "x profile" in notes.casefold() or not existing.get("image_file"):
            existing["image_file"] = image_file
            existing["notes"] = notes
    if x_handle and not existing.get("x_handle"):
        existing["x_handle"] = x_handle
    existing["match_names"] = safe_match_names(
        str(existing["name"]), list(existing.get("aliases") or [])
    )
    for key in {collapse_key(str(existing["name"])), *[collapse_key(a) for a in existing["aliases"]]}:
        if key:
            index[key] = existing
    return existing, created


def ensure_work(
    works: list[dict[str, Any]],
    *,
    work_id: str,
    title: str,
    studio: str,
    actors: list[str],
    tags: list[str],
    year: int | None,
    plot: str | None,
) -> bool:
    for item in works:
        if item.get("id") == work_id:
            item["actors"] = list(dict.fromkeys([*actors, *(item.get("actors") or [])]))
            item["tags"] = list(dict.fromkeys([*tags, *(item.get("tags") or [])]))
            return False
    works.append(
        {
            "id": work_id,
            "title": title,
            "original_title": title,
            "code": None,
            "family": "chinese",
            "category": "China",
            "year": year,
            "studio": studio,
            "series": studio,
            "plot": plot,
            "actors": actors,
            "tags": tags,
            "aliases": [],
        }
    )
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--refresh-archive", action="store_true", help="re-fetch gallery JSON")
    parser.add_argument("--limit", type=int, default=80, help="max adult candidates to probe")
    parser.add_argument("--min-followers", type=int, default=400_000)
    parser.add_argument("--scenes-per-site", type=int, default=12, help="max scenes to import per site")
    parser.add_argument("--skip-x-verify", action="store_true", help="do not live-verify/set x_handle")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    token = theporndb_token_from_env(ROOT / ".env")
    if not token:
        raise SystemExit("SHADOW_MDC_THEPORNDB_TOKEN missing")

    archive = load_archive(refresh=args.refresh_archive)
    candidates = adult_candidates(archive, min_followers=args.min_followers)[: args.limit]
    print(f"adult candidates probed: {len(candidates)} / archive={len(archive)}", flush=True)

    actors_doc = json.loads(ACTORS_PATH.read_text(encoding="utf-8"))
    works_doc = json.loads(WORKS_PATH.read_text(encoding="utf-8"))
    actors: list[dict[str, Any]] = list(actors_doc.get("actors") or [])
    works: list[dict[str, Any]] = list(works_doc.get("works") or [])
    index = build_actor_index(actors)

    before_actors = len(actors)
    before_works = len(works)
    before_blogger = sum(1 for a in actors if "blogger" in (a.get("groups") or []))
    before_x = sum(1 for a in actors if a.get("x_handle"))

    stats = {
        "handles_scraped": len(archive),
        "candidates": len(candidates),
        "with_tpdb_works": 0,
        "actors_created": 0,
        "actors_updated": 0,
        "works_added": 0,
        "posters": 0,
        "avatars": 0,
        "x_verified": 0,
        "x_failed": 0,
    }

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    with httpx.Client(timeout=35.0, follow_redirects=True, headers=headers) as client:
        x_client = httpx.Client(timeout=12.0, follow_redirects=True)
        try:
            for rank, row in enumerate(candidates, start=1):
                sn = str(row["screen_name"])
                display = clean_display_name(str(row.get("name") or ""), sn)
                print(f"[{rank}/{len(candidates)}] @{sn} ({display})", flush=True)

                real_sites = discover_real_sites(client, sn)
                q_scenes = scene_q_hits(client, sn, display)
                # Collect scenes from real sites.
                scenes: list[dict[str, Any]] = []
                site_meta: dict[str, str] = {}
                for short in real_sites:
                    batch = fetch_site_scenes(
                        client, short, pages=max(1, (args.scenes_per_site + 49) // 50), per_page=50
                    )[: args.scenes_per_site]
                    for scene in batch:
                        scenes.append(scene)
                        site = scene.get("site") or {}
                        if isinstance(site, dict):
                            site_meta[short] = str(site.get("name") or short)
                # Add unique q hits.
                seen_ids = {
                    str(scene.get("id") or scene.get("_id") or "")
                    for scene in scenes
                }
                for scene in q_scenes:
                    sid = str(scene.get("id") or scene.get("_id") or "")
                    if sid and sid in seen_ids:
                        continue
                    if sid:
                        seen_ids.add(sid)
                    scenes.append(scene)
                    site = scene.get("site") or {}
                    if isinstance(site, dict) and site.get("short_name"):
                        real_sites.append(str(site["short_name"]))
                        site_meta[str(site["short_name"])] = str(site.get("name") or site["short_name"])

                # Deduplicate sites list
                real_sites = list(dict.fromkeys(real_sites))
                if not scenes:
                    print(f"  no TPDB works", flush=True)
                    continue

                stats["with_tpdb_works"] += 1
                print(f"  sites={real_sites} scenes={len(scenes)}", flush=True)

                aliases = [sn, f"@{sn}", str(row.get("name") or "")]
                aliases = [a for a in dict.fromkeys(aliases) if a and a != display]

                # Avatar from X (real public profile image).
                image_file = None
                avatar_url = str(row.get("avatar_url") or "")
                content = download_image(client, avatar_url)
                notes = "Imported from nv-pu-sa X gallery + ThePornDB works."
                if content is not None:
                    image_file = save_actor_image(display, content)
                    notes = "X profile avatar from nv-pu-sa gallery; works from ThePornDB."
                    stats["avatars"] += 1

                # Live-verify X handle.
                x_handle = None
                if not args.skip_x_verify:
                    normalized = normalize_x_handle(sn)
                    if normalized and verify_x_handle_exists(normalized, client=x_client):
                        x_handle = normalized
                        stats["x_verified"] += 1
                    else:
                        stats["x_failed"] += 1
                        print(f"  x verify failed @{sn}", flush=True)

                bio = str(row.get("description") or "").strip() or None
                if bio and len(bio) > 600:
                    bio = bio[:597] + "..."

                groups = ["blogger", "twitter"]
                if any("onlyfans" in s for s in real_sites) or "onlyfans" in (bio or "").casefold():
                    groups.append("onlyfans")
                if any("swag" in s for s in real_sites):
                    groups.append("swag")

                actor, created = upsert_blogger(
                    actors,
                    index,
                    name=display,
                    aliases=aliases,
                    groups=groups,
                    biography=bio,
                    image_file=image_file,
                    x_handle=x_handle,
                    notes=notes,
                )
                if created:
                    stats["actors_created"] += 1
                else:
                    stats["actors_updated"] += 1
                    # Still set verified handle / avatar on existing rows.
                    if x_handle:
                        actor["x_handle"] = x_handle
                    if image_file and (
                        not actor.get("image_file")
                        or "x profile" in notes.casefold()
                        or "twitter" in notes.casefold()
                    ):
                        actor["image_file"] = image_file
                        actor["notes"] = notes
                    if "blogger" not in (actor.get("groups") or []):
                        actor["groups"] = list(dict.fromkeys([*(actor.get("groups") or []), "blogger", "twitter"]))

                actor_name = str(actor["name"])
                for scene in scenes:
                    title = str(scene.get("title") or "").strip()
                    if not title:
                        continue
                    external = str(scene.get("id") or scene.get("_id") or scene.get("slug") or "").strip()
                    if not external:
                        continue
                    site = scene.get("site") or {}
                    short = str(site.get("short_name") or "tpdb") if isinstance(site, dict) else "tpdb"
                    studio = str(site.get("name") or short) if isinstance(site, dict) else short
                    work_id = f"tpdb-{short}-{slugify(external)[:48]}"
                    added = ensure_work(
                        works,
                        work_id=work_id,
                        title=title,
                        studio=studio,
                        actors=[actor_name],
                        tags=["blogger", "nv-pu-sa", "non-jav-seed", short],
                        year=year_from_scene(scene),
                        plot=f"Imported from ThePornDB site {studio} via nv-pu-sa blogger gallery.",
                    )
                    if added:
                        stats["works_added"] += 1
                        poster_url = scene_poster_url(scene)
                        if poster_url:
                            poster = download_image(client, poster_url)
                            if poster is not None:
                                write_work_poster(work_id, poster, f"theporndb:{short}")
                                stats["posters"] += 1
        finally:
            x_client.close()

    actors.sort(key=lambda item: str(item.get("name") or "").casefold())
    actors_doc["actors"] = actors
    actors_doc["source"] = "avtor.txt+real-portraits+tpdb-chinese-refresh+nv-pu-sa-bloggers"
    works_doc["works"] = works
    works_doc["source"] = "curated-non-jav-seed+tpdb-chinese-refresh+nv-pu-sa-bloggers"

    after_blogger = sum(1 for a in actors if "blogger" in (a.get("groups") or []))
    after_x = sum(1 for a in actors if a.get("x_handle"))

    print("--- summary ---", flush=True)
    print(json.dumps(stats, ensure_ascii=False, indent=2), flush=True)
    print(
        f"actors {before_actors} -> {len(actors)} "
        f"(blogger {before_blogger} -> {after_blogger}); "
        f"works {before_works} -> {len(works)}; "
        f"x_handle {before_x} -> {after_x}",
        flush=True,
    )

    if args.dry_run:
        print("dry-run: not writing catalog files", flush=True)
        return

    ACTORS_PATH.write_text(json.dumps(actors_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    WORKS_PATH.write_text(json.dumps(works_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"wrote {ACTORS_PATH}", flush=True)
    print(f"wrote {WORKS_PATH}", flush=True)


if __name__ == "__main__":
    main()
