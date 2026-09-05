#!/usr/bin/env python3
"""Aggressively fill Chinese non-JAV actor avatars and work posters from public TPDB.

Local data only — does not commit media. Prefer real portraits; when none exist for a
Chinese creator, reuse a linked work poster/screenshot. Never writes solid-color or
identicon placeholders.
"""

from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import sys
import unicodedata
from pathlib import Path
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(Path(__file__).resolve().parent))

from actor_avatars import (  # noqa: E402
    detect_image_ext,
    image_filename,
    is_designed_identicon,
    is_solid_placeholder,
    normalize_name,
    notes_indicate_real_photo,
    theporndb_token_from_env,
)

ACTORS_PATH = ROOT / "data" / "non-jav-actors.json"
WORKS_PATH = ROOT / "data" / "non-jav-works.json"
IMAGE_DIR = ROOT / "data" / "actor-images"
ARTWORK_DIR = ROOT / "data" / "artwork"
DB_PATH = ROOT / "data" / "shadow-mdc.db"

_USER_AGENT = "ShadowMDC/0.1 (https://github.com/Cylunex/shadow-mdc; chinese-media-refresh)"

# Common Mandarin pinyin syllables for romanization variants.
_PINYIN_SYLLABLES = frozenset(
    """
a ai an ang ao ba bai ban bang bao bei ben beng bi bian biao bie bin bing bo bu
ca cai can cang cao ce cen ceng cha chai chan chang chao che chen cheng chi chong
chou chu chua chuai chuan chuang chui chun chuo ci cong cou cu cuan cui cun cuo
da dai dan dang dao de dei den deng di dia dian diao die ding diu dong dou du duan
dui dun duo e ei en eng er fa fan fang fei fen feng fo fou fu ga gai gan gang gao
ge gei gen geng gong gou gu gua guai guan guang gui gun guo ha hai han hang hao he
hei hen heng hong hou hu hua huai huan huang hui hun huo ji jia jian jiang jiao jie
jin jing jiong jiu ju juan jue jun ka kai kan kang kao ke ken keng kong kou ku kua
kuai kuan kuang kui kun kuo la lai lan lang lao le lei leng li lia lian liang liao
lie lin ling liu long lou lu luan lue lun luo lv ma mai man mang mao me mei men meng
mi mian miao mie min ming miu mo mou mu na nai nan nang nao ne nei nen neng ni nian
niang niao nie nin ning niu nong nou nu nuan nue nuo nv o ou pa pai pan pang pao pei
pen peng pi pian piao pie pin ping po pou pu qi qia qian qiang qiao qie qin qing
qiong qiu qu quan que qun ran rang rao re ren reng ri rong rou ru rua ruan rui run
ruo sa sai san sang sao se sen seng sha shai shan shang shao she shei shen sheng shi
shou shu shua shuai shuan shuang shui shun shuo si song sou su suan sui sun suo ta
tai tan tang tao te teng ti tian tiao tie ting tong tou tu tuan tui tun tuo wa wai
wan wang wei wen weng wo wu xi xia xian xiang xiao xie xin xing xiong xiu xu xuan
xue xun ya yan yang yao ye yi yin ying yo yong you yu yuan yue yun za zai zan zang
zao ze zei zen zeng zha zhai zhan zhang zhao zhe zhei zhen zheng zhi zhong zhou zhu
zhua zhuai zhuan zhuang zhui zhun zhuo zi zong zou zu zuan zui zun zuo
""".split()
)

_STUDIO_NAME_MARKERS = (
    "传媒",
    "制片",
    "探花",
    "厂",
    "SWAG",
    "OnlyFans",
    "福利",
    "博主",
    "精东",
    "皇家华人",
    "起点",
    "大象",
    "蜜桃",
    "乐播",
    "星空无限",
    "麻豆传媒",
    "天美传媒",
    "果冻传媒",
)

# TPDB site short_names that carry Chinese / Madou-adjacent catalog.
_CHINESE_SITES: tuple[tuple[str, str, str], ...] = (
    ("modelmediaasia", "麻豆传媒", "madou"),
    ("fansdbhongkongdollonlyfans", "HongKongDoll", "onlyfans"),
)


def collapse_key(value: str) -> str:
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalize_name(value))


def segment_pinyin_token(token: str) -> list[str]:
    lowered = token.casefold()
    if not lowered.isascii() or not lowered.isalpha():
        return [token]
    out: list[str] = []
    index = 0
    while index < len(lowered):
        matched: str | None = None
        for length in range(min(6, len(lowered) - index), 0, -1):
            piece = lowered[index : index + length]
            if piece in _PINYIN_SYLLABLES:
                matched = piece
                break
        if matched is None:
            return [token]
        out.append(matched.capitalize() if token[:1].isupper() else matched)
        index += len(matched)
    return out


def romanization_variants(name: str, aliases: list[str]) -> list[str]:
    ordered: list[str] = []

    def _push(value: str) -> None:
        cleaned = " ".join(unicodedata.normalize("NFKC", value).split())
        if cleaned and cleaned not in ordered:
            ordered.append(cleaned)

    for raw in (name, *aliases):
        _push(raw)
        camel = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", raw)
        camel = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", camel)
        _push(camel)
        parts: list[str] = []
        changed = False
        for token in camel.split():
            if token.isascii() and token.isalpha() and len(token) >= 4:
                segs = segment_pinyin_token(token)
                if len(segs) > 1:
                    parts.extend(segs)
                    changed = True
                    continue
            parts.append(token)
        if changed:
            _push(" ".join(parts))
    return [
        item
        for item in ordered
        if sum(ch.isalpha() and ch.isascii() for ch in item) >= 4
    ]


def _download_image(client: httpx.Client, url: str) -> bytes | None:
    if not isinstance(url, str) or not url.startswith("https://"):
        return None
    try:
        response = client.get(url)
        response.raise_for_status()
    except httpx.HTTPError:
        return None
    content = response.content
    if detect_image_ext(content) is None or len(content) < 2500:
        return None
    return content


def _tpdb_image_url(row: dict[str, Any]) -> str | None:
    for key in ("image", "face", "thumbnail"):
        value = row.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
        if isinstance(value, dict):
            for nested in ("url", "full", "large", "medium"):
                candidate = value.get(nested)
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    return candidate
    posters = row.get("posters")
    if isinstance(posters, list):
        for poster in posters[:3]:
            if isinstance(poster, str) and poster.startswith("https://"):
                return poster
            if isinstance(poster, dict):
                for nested in ("url", "full", "large"):
                    candidate = poster.get(nested)
                    if isinstance(candidate, str) and candidate.startswith("https://"):
                        return candidate
    if isinstance(posters, dict):
        for nested in ("full", "large", "url"):
            candidate = posters.get(nested)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
    return None


def _scene_poster_url(scene: dict[str, Any]) -> str | None:
    for key in ("poster", "poster_image", "image"):
        value = scene.get(key)
        if isinstance(value, str) and value.startswith("https://"):
            return value
        if isinstance(value, dict):
            for nested in ("full", "large", "url"):
                candidate = value.get(nested)
                if isinstance(candidate, str) and candidate.startswith("https://"):
                    return candidate
    posters = scene.get("posters")
    if isinstance(posters, dict):
        for nested in ("full", "large", "url"):
            candidate = posters.get(nested)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
    if isinstance(posters, list):
        for poster in posters[:2]:
            if isinstance(poster, str) and poster.startswith("https://"):
                return poster
            if isinstance(poster, dict):
                for nested in ("url", "full"):
                    candidate = poster.get(nested)
                    if isinstance(candidate, str) and candidate.startswith("https://"):
                        return candidate
    background = scene.get("background") or scene.get("back_image")
    if isinstance(background, str) and background.startswith("https://"):
        return background
    if isinstance(background, dict):
        for nested in ("full", "large", "url"):
            candidate = background.get(nested)
            if isinstance(candidate, str) and candidate.startswith("https://"):
                return candidate
    return None


def _scene_performer_names(scene: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for item in scene.get("performers") or []:
        if isinstance(item, str) and item.strip():
            names.append(item.strip())
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name")
        if not name and isinstance(item.get("performer"), dict):
            name = item["performer"].get("name")
        if isinstance(name, str) and name.strip() and name.strip() not in names:
            names.append(name.strip())
    return names


def _looks_studio_name(name: str) -> bool:
    return any(marker in name for marker in _STUDIO_NAME_MARKERS)


def _slugify(value: str) -> str:
    text = unicodedata.normalize("NFKC", value).strip().casefold()
    text = re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "-", text)
    return text.strip("-")[:80] or "item"


def _is_real_image_file(path: Path) -> bool:
    if not path.is_file() or path.stat().st_size < 2500:
        return False
    if is_solid_placeholder(path) or is_designed_identicon(path):
        return False
    # Reject tiny square avatar-copies used as fake posters (256x256).
    try:
        from PIL import Image
    except ImportError:
        return path.stat().st_size >= 8000
    with Image.open(path) as image:
        width, height = image.size
    if width == 256 and height == 256:
        return False
    if path.stat().st_size < 8000 and max(width, height) < 400:
        return False
    return True


def _build_actor_index(actors: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    index: dict[str, dict[str, Any]] = {}
    for actor in actors:
        keys = {collapse_key(str(actor["name"]))}
        for alias in actor.get("aliases") or []:
            keys.add(collapse_key(str(alias)))
        for match_name in actor.get("match_names") or []:
            keys.add(collapse_key(str(match_name)))
        for key in keys:
            if key and key not in index:
                index[key] = actor
    return index


def _exact_actor_match(
    index: dict[str, dict[str, Any]], candidate: str
) -> dict[str, Any] | None:
    return index.get(collapse_key(candidate))


def fetch_site_scenes(
    client: httpx.Client, site: str, *, pages: int = 8, per_page: int = 50
) -> list[dict[str, Any]]:
    scenes: list[dict[str, Any]] = []
    for page in range(1, pages + 1):
        try:
            response = client.get(
                "https://api.theporndb.net/scenes",
                params={"site": site, "per_page": per_page, "page": page},
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
        scenes.extend(item for item in batch if isinstance(item, dict))
        if len(batch) < per_page:
            break
    return scenes


def fetch_performer_by_name(
    client: httpx.Client, query: str
) -> dict[str, Any] | None:
    try:
        response = client.get(
            "https://api.theporndb.net/performers",
            params={"q": query, "per_page": 10},
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        rows = response.json().get("data") or []
    except ValueError:
        return None
    if not isinstance(rows, list):
        return None
    wanted = collapse_key(query)
    best: dict[str, Any] | None = None
    for row in rows:
        if not isinstance(row, dict):
            continue
        names = {collapse_key(str(row.get("name") or ""))}
        for alias in row.get("aliases") or []:
            if isinstance(alias, str):
                names.add(collapse_key(alias))
            elif isinstance(alias, dict) and isinstance(alias.get("name"), str):
                names.add(collapse_key(alias["name"]))
        if wanted in names:
            if _tpdb_image_url(row):
                return row
            best = best or row
    return best


def save_actor_image(name: str, content: bytes) -> str:
    IMAGE_DIR.mkdir(parents=True, exist_ok=True)
    ext = detect_image_ext(content) or ".jpg"
    filename = image_filename(name, ext)
    digest = hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()
    for path in IMAGE_DIR.glob(f"{digest}.*"):
        if path.name != filename:
            path.unlink(missing_ok=True)
    (IMAGE_DIR / filename).write_bytes(content)
    return filename


def upsert_chinese_actor(
    actors: list[dict[str, Any]],
    index: dict[str, dict[str, Any]],
    *,
    preferred_name: str,
    aliases: list[str],
    groups: list[str],
    image_file: str | None,
    notes: str,
) -> dict[str, Any]:
    existing = _exact_actor_match(index, preferred_name)
    if existing is None:
        for alias in aliases:
            existing = _exact_actor_match(index, alias)
            if existing is not None:
                break
    if existing is None:
        actor: dict[str, Any] = {
            "name": preferred_name,
            "aliases": list(dict.fromkeys(aliases)),
            "groups": list(dict.fromkeys(groups or ["madou"])),
            "categories": ["China"],
            "match_names": [],
            "image_file": image_file,
            "biography": "Seeded from ThePornDB Chinese/Model Media catalogue.",
            "notes": notes,
        }
        actors.append(actor)
        for key in {collapse_key(preferred_name), *[collapse_key(a) for a in aliases]}:
            if key:
                index[key] = actor
        return actor

    merged_aliases = list(
        dict.fromkeys([*(existing.get("aliases") or []), *aliases, preferred_name])
    )
    # Keep primary Chinese name when present.
    if existing["name"] != preferred_name and any(
        "\u4e00" <= ch <= "\u9fff" for ch in existing["name"]
    ):
        if preferred_name not in merged_aliases:
            merged_aliases.append(preferred_name)
    elif any("\u4e00" <= ch <= "\u9fff" for ch in preferred_name) and not any(
        "\u4e00" <= ch <= "\u9fff" for ch in str(existing["name"])
    ):
        if existing["name"] not in merged_aliases:
            merged_aliases.append(str(existing["name"]))
        existing["name"] = preferred_name
    existing["aliases"] = [item for item in merged_aliases if item != existing["name"]]
    groups_now = list(dict.fromkeys([*(existing.get("groups") or []), *groups]))
    existing["groups"] = groups_now
    cats = list(dict.fromkeys([*(existing.get("categories") or []), "China"]))
    existing["categories"] = cats
    if image_file and (
        not existing.get("image_file")
        or not notes_indicate_real_photo(str(existing.get("notes") or ""))
    ):
        existing["image_file"] = image_file
        existing["notes"] = notes
    elif image_file and not existing.get("image_file"):
        existing["image_file"] = image_file
        existing["notes"] = notes
    for key in {collapse_key(str(existing["name"])), *[collapse_key(a) for a in existing["aliases"]]}:
        if key:
            index[key] = existing
    return existing


def chinese_display_name(tpdb_name: str, index: dict[str, dict[str, Any]]) -> str:
    matched = _exact_actor_match(index, tpdb_name)
    if matched is not None:
        return str(matched["name"])
    return tpdb_name


def ensure_work_entry(
    works: list[dict[str, Any]],
    *,
    work_id: str,
    title: str,
    original_title: str | None,
    code: str | None,
    studio: str,
    actors: list[str],
    tags: list[str],
    year: int | None,
    plot: str | None,
) -> dict[str, Any]:
    for item in works:
        if item.get("id") == work_id:
            # Refresh actors/tags lightly.
            item["actors"] = list(dict.fromkeys([*actors, *(item.get("actors") or [])]))
            item["tags"] = list(dict.fromkeys([*tags, *(item.get("tags") or [])]))
            if code and not item.get("code"):
                item["code"] = code
            return item
    entry = {
        "id": work_id,
        "title": title,
        "original_title": original_title or title,
        "code": code,
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
    works.append(entry)
    return entry


def write_work_poster(work_uuid: str, content: bytes, source: str) -> Path:
    target_dir = ARTWORK_DIR / work_uuid
    target_dir.mkdir(parents=True, exist_ok=True)
    # Remove placeholder-sized posters.
    for path in target_dir.glob("poster.*"):
        if not _is_real_image_file(path):
            path.unlink(missing_ok=True)
    ext = detect_image_ext(content) or ".jpg"
    target = target_dir / f"poster{ext}"
    target.write_bytes(content)
    marker = target_dir / "source.txt"
    marker.write_text(source + "\n", encoding="utf-8")
    return target


def db_seed_identity_map(connection: sqlite3.Connection) -> dict[str, str]:
    rows = connection.execute(
        """
        SELECT value, work_id FROM external_identities
        WHERE provider = 'non-jav-seed' AND kind = 'provider_id'
        """
    ).fetchall()
    return {str(value): str(work_id) for value, work_id in rows}


def db_upsert_work_artwork(
    connection: sqlite3.Connection, work_id: str, poster_path: Path, seed_id: str
) -> None:
    row = connection.execute(
        "SELECT artwork FROM works WHERE id = ?", (work_id,)
    ).fetchone()
    if row is None:
        return
    try:
        artwork = json.loads(row[0] or "[]")
    except json.JSONDecodeError:
        artwork = []
    retained = [
        item
        for item in artwork
        if isinstance(item, dict) and item.get("kind") not in {"poster", "thumb"}
    ]
    artwork = [
        {
            "kind": "poster",
            "local_path": str(poster_path),
            "source": "theporndb",
            "seed_id": seed_id,
        },
        *retained,
    ]
    connection.execute(
        "UPDATE works SET artwork = ? WHERE id = ?",
        (json.dumps(artwork, ensure_ascii=False), work_id),
    )


def main() -> None:
    token = theporndb_token_from_env(ROOT / ".env")
    if not token:
        raise SystemExit("SHADOW_MDC_THEPORNDB_TOKEN missing")

    actors_doc = json.loads(ACTORS_PATH.read_text(encoding="utf-8"))
    works_doc = json.loads(WORKS_PATH.read_text(encoding="utf-8"))
    actors: list[dict[str, Any]] = list(actors_doc.get("actors") or [])
    works: list[dict[str, Any]] = list(works_doc.get("works") or [])
    index = _build_actor_index(actors)

    before_actors_with_img = sum(
        1
        for actor in actors
        if "China" in (actor.get("categories") or []) and actor.get("image_file")
    )
    before_china_works = sum(
        1 for work in works if work.get("category") == "China" or work.get("family") == "chinese"
    )

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "User-Agent": _USER_AGENT,
    }

    stats = {
        "scenes_seen": 0,
        "works_added": 0,
        "posters_downloaded": 0,
        "actor_portraits": 0,
        "actors_added": 0,
        "avatars_from_posters": 0,
        "existing_placeholder_posters_replaced": 0,
    }

    connection = sqlite3.connect(DB_PATH)
    identity_map = db_seed_identity_map(connection)

    with httpx.Client(timeout=30.0, follow_redirects=True, headers=headers) as client:
        # --- Pass 1: site scenes → works + posters + performer portraits ---
        for site, studio, group in _CHINESE_SITES:
            print(f"fetch site={site}", flush=True)
            scenes = fetch_site_scenes(client, site, pages=10 if site == "modelmediaasia" else 4)
            print(f"  scenes={len(scenes)}", flush=True)
            for scene in scenes:
                stats["scenes_seen"] += 1
                title = str(scene.get("title") or "").strip()
                if not title:
                    continue
                external = (
                    str(scene.get("id") or scene.get("_id") or scene.get("slug") or "").strip()
                )
                if not external:
                    continue
                code = None
                for key in ("code", "external_id"):
                    value = scene.get(key)
                    if isinstance(value, str) and value.strip():
                        code = value.strip()
                        break
                # Madou codes often appear in title like MD-0265
                if code is None:
                    match = re.search(r"\b(MD[A-Z]*[-_]?\d{2,5})\b", title, flags=re.I)
                    if match:
                        code = match.group(1).upper().replace("_", "-")

                work_id = f"tpdb-{site}-{_slugify(external)[:48]}"
                performer_names = _scene_performer_names(scene)
                local_actor_names: list[str] = []
                for perf in performer_names:
                    before_count = len(actors)
                    display = chinese_display_name(perf, index)
                    aliases = [perf]
                    if display != perf:
                        aliases.append(display)
                    # Try portrait from embedded scene performer payload first.
                    image_file = None
                    notes = "Portrait from ThePornDB Model Media / Chinese scene performer."
                    # Fetch canonical performer for a stable image when missing.
                    existing = _exact_actor_match(index, perf) or _exact_actor_match(index, display)
                    need_image = existing is None or not existing.get("image_file")
                    if need_image:
                        row = fetch_performer_by_name(client, perf)
                        url = _tpdb_image_url(row) if row else None
                        if url:
                            content = _download_image(client, url)
                            if content is not None:
                                image_file = save_actor_image(display, content)
                                stats["actor_portraits"] += 1
                    actor = upsert_chinese_actor(
                        actors,
                        index,
                        preferred_name=display if any("\u4e00" <= ch <= "\u9fff" for ch in display) else (
                            existing["name"] if existing else display
                        ),
                        aliases=aliases,
                        groups=[group, "madou"] if group == "madou" else [group, "blogger"],
                        image_file=image_file,
                        notes=notes if image_file else (
                            str((existing or {}).get("notes") or "Awaiting portrait; may use work cover.")
                        ),
                    )
                    if len(actors) > before_count:
                        stats["actors_added"] += 1
                    local_actor_names.append(str(actor["name"]))

                if not local_actor_names:
                    # Still keep studio-level work for cover inventory.
                    local_actor_names = [studio]

                year = None
                date_value = scene.get("date") or scene.get("release_date")
                if isinstance(date_value, str) and len(date_value) >= 4 and date_value[:4].isdigit():
                    year = int(date_value[:4])

                existed_ids = {item["id"] for item in works}
                ensure_work_entry(
                    works,
                    work_id=work_id,
                    title=title,
                    original_title=title,
                    code=code,
                    studio=studio,
                    actors=local_actor_names,
                    tags=["theporndb", site, "chinese", group],
                    year=year,
                    plot=str(scene.get("description") or scene.get("details") or "")[:500]
                    or None,
                )
                if work_id not in existed_ids:
                    stats["works_added"] += 1

                poster_url = _scene_poster_url(scene)
                if not poster_url:
                    continue
                content = _download_image(client, poster_url)
                if content is None:
                    continue

                # Prefer DB uuid when this seed id already exists; else write under seed slug
                # and let later seed_non_jav_works adopt it.
                work_uuid = identity_map.get(work_id)
                poster_key = work_uuid or work_id
                poster_path = write_work_poster(
                    poster_key, content, source=f"theporndb:{site}:{external}"
                )
                stats["posters_downloaded"] += 1
                if work_uuid:
                    db_upsert_work_artwork(connection, work_uuid, poster_path, work_id)

                # Attach poster as avatar for performers still missing a portrait.
                for actor_name in local_actor_names:
                    actor = _exact_actor_match(index, actor_name)
                    if actor is None or actor.get("image_file"):
                        continue
                    if _looks_studio_name(str(actor["name"])) and group == "madou":
                        # Brand rows can still use a classic cover.
                        pass
                    filename = save_actor_image(str(actor["name"]), content)
                    actor["image_file"] = filename
                    actor["notes"] = (
                        "Work cover/screenshot from ThePornDB Chinese scene "
                        "(no dedicated portrait found)."
                    )
                    stats["avatars_from_posters"] += 1

        # --- Pass 2: refresh portraits for existing China people via improved matching ---
        print("pass2: existing China actors without portraits", flush=True)
        for actor in list(actors):
            if "China" not in (actor.get("categories") or []):
                continue
            if actor.get("image_file"):
                current = IMAGE_DIR / str(actor["image_file"])
                if _is_real_image_file(current) and notes_indicate_real_photo(
                    str(actor.get("notes") or "")
                ):
                    continue
                if _is_real_image_file(current):
                    continue
            if _looks_studio_name(str(actor["name"])):
                continue
            queries = romanization_variants(
                str(actor["name"]), list(actor.get("aliases") or [])
            )
            if not queries:
                continue
            wanted = {
                collapse_key(item)
                for item in (str(actor["name"]), *(actor.get("aliases") or []), *queries)
            }
            found_url = None
            for query in queries[:6]:
                row = fetch_performer_by_name(client, query)
                if row is None:
                    continue
                row_names = {collapse_key(str(row.get("name") or ""))}
                for alias in row.get("aliases") or []:
                    if isinstance(alias, str):
                        row_names.add(collapse_key(alias))
                if not (row_names & wanted):
                    continue
                found_url = _tpdb_image_url(row)
                if found_url:
                    break
            if not found_url:
                continue
            content = _download_image(client, found_url)
            if content is None:
                continue
            filename = save_actor_image(str(actor["name"]), content)
            actor["image_file"] = filename
            actor["notes"] = "Portrait from ThePornDB performer image."
            stats["actor_portraits"] += 1

        # --- Pass 3: replace placeholder posters on curated China seed works ---
        print("pass3: replace curated China placeholder posters", flush=True)
        china_seed_works = [
            work
            for work in works
            if work.get("category") == "China" or work.get("family") == "chinese"
        ]
        for work in china_seed_works:
            seed_id = str(work["id"])
            work_uuid = identity_map.get(seed_id)
            poster_dir = ARTWORK_DIR / (work_uuid or seed_id)
            existing_poster = None
            if poster_dir.is_dir():
                for path in poster_dir.glob("poster.*"):
                    if _is_real_image_file(path):
                        existing_poster = path
                        break
            if existing_poster is not None:
                continue
            # Search TPDB by primary actor romanization / title keywords.
            actor_names = list(work.get("actors") or [])
            queries: list[str] = []
            for actor_name in actor_names:
                actor = _exact_actor_match(index, str(actor_name))
                if actor is None:
                    queries.extend(romanization_variants(str(actor_name), []))
                else:
                    queries.extend(
                        romanization_variants(
                            str(actor["name"]), list(actor.get("aliases") or [])
                        )
                    )
            if work.get("code"):
                queries.insert(0, str(work["code"]))
            queries.append(str(work.get("title") or ""))
            scene_hit = None
            for query in queries[:8]:
                query = " ".join(str(query).split())
                if len(query) < 3:
                    continue
                try:
                    response = client.get(
                        "https://api.theporndb.net/scenes",
                        params={"q": query, "per_page": 8},
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
                    site_name = ""
                    if isinstance(site, dict):
                        site_name = str(site.get("name") or site.get("short_name") or "")
                    # Prefer Chinese-ish sites when possible.
                    if site_name and not any(
                        marker in site_name.casefold()
                        for marker in (
                            "model media",
                            "madou",
                            "swag",
                            "fansdb",
                            "hongkong",
                            "jelly",
                            "asia",
                        )
                    ):
                        # still allow if performer matches
                        perf_keys = {collapse_key(n) for n in _scene_performer_names(row)}
                        actor_keys = {collapse_key(n) for n in actor_names}
                        actor_keys |= {
                            collapse_key(a)
                            for n in actor_names
                            for a in (
                                (_exact_actor_match(index, str(n)) or {}).get("aliases") or []
                            )
                        }
                        if not (perf_keys & {k for k in actor_keys if k}):
                            continue
                    if _scene_poster_url(row):
                        scene_hit = row
                        break
                if scene_hit is not None:
                    break
            if scene_hit is None:
                # Fallback: copy a real actor portrait as temporary cover only if portrait
                # itself is a real photo (not already a cover loop). Prefer skipping.
                continue
            url = _scene_poster_url(scene_hit)
            content = _download_image(client, url) if url else None
            if content is None:
                continue
            poster_key = work_uuid or seed_id
            poster_path = write_work_poster(
                poster_key, content, source=f"theporndb:curated-replace:{seed_id}"
            )
            stats["existing_placeholder_posters_replaced"] += 1
            if work_uuid:
                db_upsert_work_artwork(connection, work_uuid, poster_path, seed_id)

            for actor_name in actor_names:
                actor = _exact_actor_match(index, str(actor_name))
                if actor is None or actor.get("image_file"):
                    continue
                filename = save_actor_image(str(actor["name"]), content)
                actor["image_file"] = filename
                actor["notes"] = (
                    "Work cover/screenshot from ThePornDB (no dedicated portrait found)."
                )
                stats["avatars_from_posters"] += 1

        # --- Pass 4: HongKongDoll / blogger special-case scene search ---
        for blogger, query in (
            ("HongKongDoll", "HongKongDoll"),
            ("HongKongDoll", "Hong Kong Doll"),
        ):
            actor = _exact_actor_match(index, blogger)
            if actor is None:
                continue
            if actor.get("image_file") and _is_real_image_file(
                IMAGE_DIR / str(actor["image_file"])
            ):
                continue
            try:
                response = client.get(
                    "https://api.theporndb.net/scenes",
                    params={"q": query, "per_page": 5},
                )
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            rows = response.json().get("data") or []
            for row in rows:
                if not isinstance(row, dict):
                    continue
                url = _scene_poster_url(row)
                content = _download_image(client, url) if url else None
                if content is None:
                    continue
                filename = save_actor_image(str(actor["name"]), content)
                actor["image_file"] = filename
                actor["notes"] = (
                    "Work cover/screenshot from ThePornDB FansDB/HongKongDoll scene."
                )
                stats["avatars_from_posters"] += 1
                break

    # Rebuild match_names lightly for touched China actors.
    for actor in actors:
        if "China" not in (actor.get("categories") or []):
            continue
        names = [str(actor["name"]), *[str(x) for x in actor.get("aliases") or []]]
        match_names = []
        for item in names:
            cleaned = " ".join(unicodedata.normalize("NFKC", item).split())
            if cleaned and cleaned not in match_names:
                match_names.append(cleaned)
        actor["match_names"] = match_names

    actors.sort(key=lambda item: str(item["name"]).casefold())
    works.sort(key=lambda item: str(item["id"]).casefold())
    actors_doc["actors"] = actors
    actors_doc["source"] = "avtor.txt+real-portraits+tpdb-chinese-refresh"
    works_doc["works"] = works
    works_doc["source"] = "curated-non-jav-seed+tpdb-chinese-refresh"
    ACTORS_PATH.write_text(
        json.dumps(actors_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    WORKS_PATH.write_text(
        json.dumps(works_doc, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    connection.commit()
    connection.close()

    after_actors_with_img = sum(
        1
        for actor in actors
        if "China" in (actor.get("categories") or []) and actor.get("image_file")
    )
    after_china_works = sum(
        1 for work in works if work.get("category") == "China" or work.get("family") == "chinese"
    )
    print("STATS", json.dumps(stats, ensure_ascii=False))
    print(
        f"China actors with image: {before_actors_with_img} -> {after_actors_with_img} "
        f"(total China actors {sum(1 for a in actors if 'China' in (a.get('categories') or []))})"
    )
    print(f"China works: {before_china_works} -> {after_china_works}")


if __name__ == "__main__":
    main()
