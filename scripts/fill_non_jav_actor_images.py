#!/usr/bin/env python3
"""Fill missing non-JAV actor portraits from free public indexes.

Sources, tried in order per name:

1. **GFriends** — the JAV portrait index (``Filetree.json``). Covers the many
   Japanese performers that appear in non-JAV works (VR / leaked / 无码 collections).
2. **coserslove.com** — homepage coser avatars (``img.coserslove.com/avatars``).
   Covers the cosplay creators that the name-only importer created without images.
3. **ThePornDB → Wikipedia → Wikidata → Wikimedia Commons** — via
   ``scripts/actor_avatars.fetch_real_portrait``; used for western performers.

Never writes placeholders: a download is normalised to JPEG via Pillow, rejected
when it is tiny or a near-solid image, and the catalog entry is only updated when
an actual portrait file landed on disk.

Example::

    PYTHONPATH=src .venv/bin/python scripts/fill_non_jav_actor_images.py --dry-run
    PYTHONPATH=src .venv/bin/python scripts/fill_non_jav_actor_images.py --limit 20
    PYTHONPATH=src .venv/bin/python scripts/fill_non_jav_actor_images.py --all --sources gfriends
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
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from actor_avatars import (  # noqa: E402
    detect_image_ext,
    fetch_real_portrait,
    image_filename,
    is_designed_identicon,
    is_solid_placeholder,
    theporndb_token_from_env,
)

from shadow_mdc.actress_aliases import actor_identity_forms  # noqa: E402
from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.services.gfriends import GfriendsActorImageResolver  # noqa: E402
from shadow_mdc.services.non_jav_actor_catalog import (  # noqa: E402
    NonJavActorCatalog,
    NonJavActorCatalogStore,
    NonJavActorProfile,
)

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
COSERSLOVE_HOME = "https://coserslove.com/"
_AVATAR_PATTERN = re.compile(
    r'<img src="(https://img\.coserslove\.com/avatars/[^"]+)" alt="([^"]*)"',
)
MIN_PORTRAIT_BYTES = 1200


@dataclass(frozen=True)
class Portrait:
    content: bytes
    source: str


def _normalize(value: str) -> str:
    text = unicodedata.normalize("NFKC", value or "")
    return re.sub(r"\s+", "", text.casefold())


def _lookup_keys(profile: NonJavActorProfile) -> list[str]:
    keys: list[str] = [profile.name, *profile.aliases, *profile.match_names]
    keys.extend(actor_identity_forms(profile.name))
    ordered: list[str] = []
    seen: set[str] = set()
    for item in keys:
        cleaned = (item or "").strip()
        if not cleaned:
            continue
        normalized = _normalize(cleaned)
        if normalized in seen:
            continue
        seen.add(normalized)
        ordered.append(cleaned)
    return ordered


def _to_jpeg(content: bytes) -> bytes | None:
    """Normalise any decodable image to JPEG (AVIF/WebP covers included)."""

    from PIL import Image

    try:
        with Image.open(io.BytesIO(content)) as image:
            rgb = image.convert("RGB")
            buffer = io.BytesIO()
            rgb.save(buffer, format="JPEG", quality=88, optimize=True)
            return buffer.getvalue()
    except Exception:
        return None


class CosersloveAvatars:
    """Homepage ``avatar → coser name`` map (one request, matched by name)."""

    def __init__(self, client: httpx.Client) -> None:
        self._client = client
        self._entries: dict[str, str] | None = None

    def load(self) -> dict[str, str]:
        if self._entries is not None:
            return self._entries
        entries: dict[str, str] = {}
        try:
            response = self._client.get(
                COSERSLOVE_HOME,
                headers={
                    "User-Agent": BROWSER_UA,
                    "Accept": "text/html,application/xhtml+xml",
                    "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.7",
                },
            )
            response.raise_for_status()
            for url, name in _AVATAR_PATTERN.findall(response.text):
                key = _normalize(name)
                if key and key not in entries:
                    entries[key] = url
        except httpx.HTTPError as exc:
            print(f"[coserslove] unavailable: {type(exc).__name__}: {exc}")
        self._entries = entries
        return entries

    def resolve(self, keys: list[str], entries: dict[str, str]) -> str | None:
        for key in keys:
            hit = entries.get(_normalize(key))
            if hit:
                return hit
        # Fall back to containment for decorated names ("Bangni邦尼" vs "Bangni").
        for key in keys:
            needle = _normalize(key)
            if len(needle) < 4:
                continue
            for name, url in entries.items():
                if needle in name or name in needle:
                    return url
        return None


CDN_HOST_FALLBACKS = (
    "https://cdn.jsdelivr.net/gh/gfriends/gfriends@master/",
    "https://fastly.jsdelivr.net/gh/gfriends/gfriends@master/",
    "https://raw.githubusercontent.com/gfriends/gfriends/master/",
)


def _download(client: httpx.Client, url: str) -> bytes | None:
    candidates = [url]
    if "cdn.jsdelivr.net/gh/gfriends/" in url:
        relative = url.split("gh/gfriends/gfriends@master/", 1)[-1]
        candidates.extend(base + relative for base in CDN_HOST_FALLBACKS if base + relative not in candidates)
    last_error = ""
    for candidate in candidates:
        for attempt in range(3):
            try:
                response = client.get(
                    candidate,
                    headers={"User-Agent": BROWSER_UA, "Referer": COSERSLOVE_HOME},
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if isinstance(exc, httpx.HTTPStatusError) and exc.response.status_code in {403, 429}:
                    time.sleep(1.5 * (attempt + 1))
                    continue
                break
            if len(response.content) < MIN_PORTRAIT_BYTES:
                last_error = f"too small ({len(response.content)} bytes)"
                break
            return response.content
    print(f"    download failed: {last_error}")
    return None


def _store_portrait(
    *,
    profile: NonJavActorProfile,
    portrait: Portrait,
    image_dir: Path,
    dry_run: bool,
) -> NonJavActorProfile | None:
    content = portrait.content
    if detect_image_ext(content) is None:
        converted = _to_jpeg(content)
        if converted is None:
            print(f"    {portrait.source}: undecodable image payload")
            return None
        content = converted
    suffix = detect_image_ext(content) or ".jpg"
    filename = image_filename(profile.name, suffix)
    target = image_dir / filename
    if dry_run:
        print(f"    [dry] {portrait.source} -> {filename} ({len(content)} bytes)")
        return profile.model_copy(update={"image_file": filename})
    image_dir.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(f"{target.suffix}.part")
    temporary.write_bytes(content)
    temporary.replace(target)
    if is_solid_placeholder(target) or is_designed_identicon(target):
        target.unlink(missing_ok=True)
        print(f"    {portrait.source}: rejected placeholder bytes")
        return None
    notes = f"Portrait from {portrait.source}."
    return profile.model_copy(update={"image_file": filename, "notes": notes})


def _needs_portrait(profile: NonJavActorProfile, image_dir: Path, replace_placeholders: bool) -> bool:
    if not profile.image_file:
        return True
    if not replace_placeholders:
        return False
    path = image_dir / profile.image_file
    return is_solid_placeholder(path) or is_designed_identicon(path)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=None, help="max profiles to fill")
    parser.add_argument("--name", action="append", default=None, help="only these names")
    parser.add_argument(
        "--sources",
        default="gfriends,coserslove,wikipedia",
        help="comma separated: gfriends,coserslove,wikipedia",
    )
    parser.add_argument(
        "--all",
        action="store_true",
        help="also replace existing solid/identicon placeholder portraits",
    )
    parser.add_argument("--token", default=None, help="ThePornDB token (optional)")
    arguments = parser.parse_args()

    settings = Settings()
    if arguments.data_dir is not None:
        settings = settings.model_copy(update={"data_dir": arguments.data_dir})
    settings.ensure_directories()
    data_dir = settings.data_dir
    image_dir = data_dir / "actor-images"
    sources = [item.strip() for item in arguments.sources.split(",") if item.strip()]
    wanted = {_normalize(name) for name in (arguments.name or [])}

    store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    catalog = store.load()
    targets = [
        actor
        for actor in catalog.actors
        if (not wanted or _normalize(actor.name) in wanted)
        and _needs_portrait(actor, image_dir, arguments.all)
    ]
    targets.sort(key=lambda actor: actor.name.casefold())
    if arguments.limit is not None:
        targets = targets[: max(1, arguments.limit)]
    print(f"profiles needing a portrait: {len(targets)} / {len(catalog.actors)}")

    token = arguments.token or theporndb_token_from_env(ROOT / ".env") or settings.theporndb_token
    filled: list[tuple[str, str]] = []
    missed: list[str] = []
    updated: dict[str, NonJavActorProfile] = {}

    resolver: GfriendsActorImageResolver | None = None
    if "gfriends" in sources:
        resolver = GfriendsActorImageResolver(cache_path=data_dir / "cache" / "gfriends" / "Filetree.json")
        info = resolver.refresh()
        print(f"[gfriends] index entries: {info.get('entries', '?')}")

    with httpx.Client(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        proxy=settings.proxy_url,
        headers={"User-Agent": BROWSER_UA},
    ) as client:
        cosers = CosersloveAvatars(client) if "coserslove" in sources else None
        cosers_entries = cosers.load() if cosers is not None else {}
        if cosers is not None:
            print(f"[coserslove] homepage avatars: {len(cosers_entries)}")

        for profile in targets:
            print(f"- {profile.name} (groups={list(profile.groups)})")
            keys = _lookup_keys(profile)
            portrait: Portrait | None = None

            if resolver is not None:
                url = resolver.resolve(keys)
                if url:
                    content = _download(client, url)
                    if content is not None:
                        portrait = Portrait(content=content, source="GFriends portrait index")

            if portrait is None and cosers is not None:
                url = cosers.resolve(keys, cosers_entries)
                if url:
                    content = _download(client, url)
                    if content is not None:
                        portrait = Portrait(content=content, source="coserslove.com avatar")

            if portrait is None and "wikipedia" in sources:
                photo = fetch_real_portrait(profile.name, list(profile.aliases), theporndb_token=token)
                if photo is not None:
                    portrait = Portrait(content=photo[0], source=photo[1])

            if portrait is None:
                missed.append(profile.name)
                print("    no public portrait found")
                continue

            replacement = _store_portrait(
                profile=profile,
                portrait=portrait,
                image_dir=image_dir,
                dry_run=arguments.dry_run,
            )
            if replacement is None:
                missed.append(profile.name)
                continue
            updated[profile.name] = replacement
            filled.append((profile.name, portrait.source))
            print(f"    filled from {portrait.source}")
            time.sleep(0.2)

    if resolver is not None:
        resolver.close()

    if updated and not arguments.dry_run:
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        backup = data_dir / f"non-jav-actors.json.bak-portraits-{stamp}"
        shutil.copy2(data_dir / "non-jav-actors.json", backup)
        actors = [
            updated.get(actor.name, actor) for actor in catalog.actors
        ]
        store.save(NonJavActorCatalog.model_validate(
            {"version": catalog.version, "source": catalog.source, "actors": actors}
        ))
        print(f"catalog saved; backup at {backup.name}")

    print(
        json.dumps(
            {
                "targets": len(targets),
                "filled": len(filled),
                "missed": len(missed),
                "dry_run": arguments.dry_run,
                "by_source": {
                    source: sum(1 for _, item in filled if item == source)
                    for source in sorted({item for _, item in filled})
                },
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
