"""Real public portraits for non-JAV actors (Wikipedia / Wikidata / ThePornDB).

Default seeding never writes solid-color or identicon placeholders. When no
real photo is available, leave ``image_file`` null and let the UI show initials.
"""

from __future__ import annotations

import hashlib
import re
import os
import struct
import unicodedata
import zlib
from pathlib import Path

FONT_PATH = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")

_ADULT_MARKERS = (
    "adult film",
    "pornographic",
    "porn star",
    "porn actress",
    "porn actor",
    "adult entertainer",
    "erotic film",
    "xxx",
    "onlyfans",
    "adult entertainment",
)

# Wikidata occupations commonly used for adult performers.
_ADULT_OCCUPATION_QIDS = frozenset(
    {
        "Q488111",  # pornographic actor
        "Q829472",  # (legacy alias sometimes seen)
        "Q2251687",  # erotic photography model (occasionally)
    }
)

_USER_AGENT = "ShadowMDC/0.1 (https://github.com/Cylunex/shadow-mdc; non-jav-avatar-seed)"


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def collapse_name(value: str) -> str:
    """Casefold + strip spaces/punctuation for pinyin spacing variants (Xia Qing Zi)."""
    return re.sub(r"[^a-z0-9\u4e00-\u9fff]+", "", normalize_name(value))


def image_filename(name: str, extension: str = ".png") -> str:
    digest = hashlib.sha256(normalize_name(name).encode("utf-8")).hexdigest()
    if not extension.startswith("."):
        extension = f".{extension}"
    return f"{digest}{extension}"


def initials_for(name: str) -> str:
    cleaned = " ".join(unicodedata.normalize("NFKC", name).split())
    letters = [ch for ch in cleaned if not ch.isspace() and ch not in "·・._-@"]
    if not letters:
        return "?"
    cjk = [ch for ch in letters if ord(ch) > 0x2E7F]
    if cjk:
        return "".join(cjk[:2])
    parts = [part for part in cleaned.replace("_", " ").replace("-", " ").split() if part]
    if len(parts) >= 2:
        return (parts[0][0] + parts[1][0]).upper()
    ascii_alnum = "".join(ch for ch in letters if ch.isalnum())
    if len(ascii_alnum) >= 2:
        return ascii_alnum[:2].upper()
    return letters[0].upper()


def detect_image_ext(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        return ".webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return None


def is_solid_placeholder(path: Path) -> bool:
    """True for missing/tiny/near-solid images (legacy placeholders)."""
    if not path.is_file():
        return True
    if path.stat().st_size < 1200:
        return True
    try:
        from PIL import Image
    except ImportError:
        return path.stat().st_size < 1200
    with Image.open(path) as img:
        colors = img.convert("RGB").getcolors(maxcolors=8)
    return colors is not None and len(colors) <= 2


def is_designed_identicon(path: Path) -> bool:
    """Heuristic for the old PIL identicon PNGs (many colors, modest size, PNG)."""
    if not path.is_file() or path.suffix.lower() != ".png":
        return False
    size = path.stat().st_size
    if size < 1500 or size > 80_000:
        return False
    if is_solid_placeholder(path):
        return True
    try:
        from PIL import Image
    except ImportError:
        return False
    with Image.open(path) as img:
        rgb = img.convert("RGB")
        if rgb.size != (256, 256):
            return False
        colors = rgb.getcolors(maxcolors=512)
    # Identicons use a compact palette of gradient + accent tiles.
    return colors is not None and 3 <= len(colors) <= 400


def notes_indicate_real_photo(notes: str | None) -> bool:
    text = (notes or "").casefold()
    return any(
        marker in text
        for marker in (
            "wikipedia",
            "wikimedia",
            "wikidata",
            "theporndb",
            "public summary thumbnail",
            "public portrait",
            "performer image",
            "commons file search",
            "iafd",
            "work cover",
            "work cover/screenshot",
            "model media",
            "scene performer",
        )
    )


def notes_indicate_placeholder(notes: str | None) -> bool:
    text = (notes or "").casefold()
    return any(
        marker in text
        for marker in ("identicon", "placeholder portrait", "designed identicon", "solid-color")
    )


def theporndb_token_from_env(env_file: Path | None = None) -> str | None:
    token = (os.environ.get("SHADOW_MDC_THEPORNDB_TOKEN") or "").strip()
    if token:
        return token
    path = env_file
    if path is None:
        path = Path(__file__).resolve().parents[1] / ".env"
    if not path.is_file():
        return None
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            if key.strip() != "SHADOW_MDC_THEPORNDB_TOKEN":
                continue
            cleaned = value.strip().strip("'").strip('"')
            return cleaned or None
    except OSError:
        return None
    return None


def fetch_real_portrait(
    name: str,
    aliases: list[str] | None = None,
    *,
    theporndb_token: str | None = None,
) -> tuple[bytes, str] | None:
    """Download a real public portrait, or return None.

    Preference order: ThePornDB (adult catalog) → Wikipedia summary thumb →
    Wikidata/Commons P18 → Wikimedia Commons file search.
    """
    alias_list = list(aliases or [])
    token = theporndb_token if theporndb_token is not None else theporndb_token_from_env()
    if token:
        photo = theporndb_performer_photo(name, alias_list, token)
        if photo is not None:
            return photo, "Portrait from ThePornDB performer image."
    photo = wikipedia_photo(name, alias_list)
    if photo is not None:
        return photo, "Portrait from Wikipedia/Wikimedia public summary thumbnail."
    photo = wikidata_photo(name, alias_list)
    if photo is not None:
        return photo, "Portrait from Wikidata/Wikimedia Commons (P18)."
    photo = commons_search_photo(name, alias_list)
    if photo is not None:
        return photo, "Portrait from Wikimedia Commons file search."
    return None


def wikipedia_photo(name: str, aliases: list[str]) -> bytes | None:
    try:
        import httpx
    except ImportError:
        return None
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    titles: list[str] = []
    for title in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", title).split())
        if cleaned and cleaned not in titles:
            titles.append(cleaned)
    with httpx.Client(timeout=12.0, follow_redirects=True, headers=headers) as client:
        for title in titles:
            if not _looks_searchable_person_name(title):
                continue
            content = _wikipedia_summary_image(client, title)
            if content is not None:
                return content
            # Opensearch fallback for slight title mismatches.
            try:
                response = client.get(
                    "https://en.wikipedia.org/w/api.php",
                    params={
                        "action": "opensearch",
                        "search": title,
                        "limit": 5,
                        "namespace": 0,
                        "format": "json",
                    },
                )
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            if not isinstance(payload, list) or len(payload) < 2:
                continue
            for hit in payload[1]:
                if not isinstance(hit, str):
                    continue
                if hit in titles:
                    continue
                content = _wikipedia_summary_image(client, hit)
                if content is not None:
                    return content
    return None


def wikidata_photo(name: str, aliases: list[str]) -> bytes | None:
    try:
        import httpx
    except ImportError:
        return None
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    queries: list[str] = []
    for title in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", title).split())
        if cleaned and cleaned not in queries and _looks_searchable_person_name(cleaned):
            queries.append(cleaned)
    with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
        for query in queries:
            try:
                response = client.get(
                    "https://www.wikidata.org/w/api.php",
                    params={
                        "action": "wbsearchentities",
                        "search": query,
                        "language": "en",
                        "format": "json",
                        "limit": 5,
                        "type": "item",
                    },
                )
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                hits = response.json().get("search") or []
            except ValueError:
                continue
            if not isinstance(hits, list):
                continue
            for hit in hits:
                if not isinstance(hit, dict):
                    continue
                qid = hit.get("id")
                if not isinstance(qid, str):
                    continue
                description = str(hit.get("description") or "")
                if not _adult_description(description) and not _wikidata_has_adult_occupation(
                    client, qid
                ):
                    continue
                filename = _wikidata_image_filename(client, qid)
                if not filename:
                    continue
                content = _commons_thumb_bytes(client, filename)
                if content is not None:
                    return content
    return None


def _tpdb_image_urls(row: dict) -> list[str]:
    """Collect candidate HTTPS image URLs from a ThePornDB performer row."""
    urls: list[str] = []

    def _push(value: object) -> None:
        if isinstance(value, str) and value.startswith("https://") and value not in urls:
            urls.append(value)
        elif isinstance(value, dict):
            for key in ("url", "full", "large", "medium", "small"):
                _push(value.get(key))

    for key in ("image", "face", "thumbnail"):
        _push(row.get(key))
    posters = row.get("posters")
    if isinstance(posters, list):
        for poster in posters[:3]:
            if isinstance(poster, dict):
                _push(poster.get("url"))
    return urls


def _tpdb_row_names(row: dict) -> set[str]:
    names: set[str] = set()
    for key in ("name", "full_name"):
        value = row.get(key)
        if isinstance(value, str) and value.strip():
            names.add(normalize_name(value))
    aliases = row.get("aliases")
    if isinstance(aliases, list):
        for alias in aliases:
            if isinstance(alias, str) and alias.strip():
                names.add(normalize_name(alias))
            elif isinstance(alias, dict):
                for key in ("name", "value", "alias"):
                    val = alias.get(key)
                    if isinstance(val, str) and val.strip():
                        names.add(normalize_name(val))
    return names


def theporndb_performer_photo(name: str, aliases: list[str], token: str) -> bytes | None:
    try:
        import httpx
    except ImportError:
        return None
    headers = {
        "User-Agent": _USER_AGENT,
        "Accept": "application/json",
        "Authorization": f"Bearer {token}",
    }
    queries: list[str] = []
    for title in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", title).split())
        if cleaned and cleaned not in queries and _looks_searchable_person_name(cleaned):
            queries.append(cleaned)
            # Light variants: strip parentheticals / stage disambiguators.
            if "(" in cleaned:
                stripped = cleaned.split("(", 1)[0].strip()
                if stripped and stripped not in queries and _looks_searchable_person_name(stripped):
                    queries.append(stripped)
            # CamelCase / glued pinyin → spaced tokens (HongKongDoll, XiaQingZi).
            camel = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", cleaned)
            camel = re.sub(r"(?<=[A-Z])(?=[A-Z][a-z])", " ", camel)
            if camel != cleaned and camel not in queries and _looks_searchable_person_name(camel):
                queries.append(camel)
    wanted = {normalize_name(item) for item in queries}
    with httpx.Client(timeout=20.0, follow_redirects=True, headers=headers) as client:
        for query in queries:
            try:
                response = client.get(
                    "https://api.theporndb.net/performers",
                    params={"q": query, "per_page": 10},
                )
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            rows = payload.get("data") if isinstance(payload, dict) else None
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_names = _tpdb_row_names(row)
                query_norm = normalize_name(query)
                row_collapsed = {collapse_name(name) for name in row_names}
                wanted_collapsed = {collapse_name(name) for name in wanted}
                if not (row_names & wanted) and not (row_collapsed & wanted_collapsed):
                    # Allow close matches when query is contained in performer name.
                    if not any(query_norm in row_name or row_name in query_norm for row_name in row_names):
                        if not any(
                            collapse_name(query) in item or item in collapse_name(query)
                            for item in row_collapsed
                            if len(item) >= 4
                        ):
                            continue
                for image_url in _tpdb_image_urls(row):
                    try:
                        image = client.get(image_url)
                        image.raise_for_status()
                    except httpx.HTTPError:
                        continue
                    if detect_image_ext(image.content) is None or len(image.content) < 2000:
                        continue
                    return image.content
    return None



def commons_search_photo(name: str, aliases: list[str]) -> bytes | None:
    """Search Wikimedia Commons file titles for an adult-performer portrait."""
    try:
        import httpx
    except ImportError:
        return None
    headers = {"User-Agent": _USER_AGENT, "Accept": "application/json"}
    queries: list[str] = []
    for title in [name, *aliases]:
        cleaned = " ".join(unicodedata.normalize("NFKC", title).split())
        if cleaned and cleaned not in queries and _looks_searchable_person_name(cleaned):
            queries.append(cleaned)
    with httpx.Client(timeout=15.0, follow_redirects=True, headers=headers) as client:
        for query in queries:
            # Prefer files whose titles include the performer name + adult context.
            search_terms = [
                f'"{query}" pornographic',
                f'"{query}" porn',
                f"{query} adult film",
                query,
            ]
            for term in search_terms:
                try:
                    response = client.get(
                        "https://commons.wikimedia.org/w/api.php",
                        params={
                            "action": "query",
                            "list": "search",
                            "srsearch": term,
                            "srnamespace": 6,  # File:
                            "srlimit": 8,
                            "format": "json",
                        },
                    )
                except httpx.HTTPError:
                    continue
                if response.status_code != 200:
                    continue
                try:
                    hits = response.json().get("query", {}).get("search") or []
                except ValueError:
                    continue
                if not isinstance(hits, list):
                    continue
                query_norm = normalize_name(query)
                for hit in hits:
                    if not isinstance(hit, dict):
                        continue
                    title = str(hit.get("title") or "")
                    title_norm = normalize_name(title)
                    # Require the person name tokens to appear in the file title.
                    tokens = [tok for tok in query_norm.replace("-", " ").split() if len(tok) > 1]
                    if tokens and not all(tok in title_norm for tok in tokens):
                        continue
                    snippet = normalize_name(str(hit.get("snippet") or ""))
                    adultish = _adult_description(title) or _adult_description(snippet) or any(
                        marker in title_norm
                        for marker in ("porn", "adult", "xxx", "avn", "xbiz", "playboy")
                    )
                    # Without adult markers, only accept exact "Name" portrait-style titles.
                    if not adultish and f"file:{query_norm}" not in title_norm:
                        continue
                    filename = title.removeprefix("File:").removeprefix("file:")
                    content = _commons_thumb_bytes(client, filename)
                    if content is not None:
                        return content
    return None


def _looks_searchable_person_name(value: str) -> bool:
    cleaned = value.strip()
    if len(cleaned) < 2:
        return False
    latin = sum(1 for ch in cleaned if ("a" <= ch.lower() <= "z"))
    if latin >= 4:
        return True
    # Allow CJK personal names (2–4 chars) but skip long studio slogans.
    non_space = [ch for ch in cleaned if not ch.isspace()]
    if not non_space:
        return False
    if all(ord(ch) > 0x2E7F for ch in non_space) and 2 <= len(non_space) <= 4:
        return True
    return False


def _adult_description(text: str) -> bool:
    lowered = text.casefold()
    return any(marker in lowered for marker in _ADULT_MARKERS)


def _wikipedia_summary_image(client: object, title: str) -> bytes | None:
    import httpx

    assert isinstance(client, httpx.Client)
    slug = title.replace(" ", "_")
    try:
        response = client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}")
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        payload = response.json()
    except ValueError:
        return None
    if not isinstance(payload, dict):
        return None
    extract = str(payload.get("extract") or payload.get("description") or "")
    if not _adult_description(extract):
        return None
    thumb = payload.get("thumbnail") or payload.get("originalimage") or {}
    source = thumb.get("source") if isinstance(thumb, dict) else None
    if not isinstance(source, str) or not source.startswith("https://"):
        return None
    if "upload.wikimedia.org" not in source and "wikipedia.org" not in source:
        return None
    try:
        image = client.get(source)
        image.raise_for_status()
    except httpx.HTTPError:
        return None
    if detect_image_ext(image.content) is None or len(image.content) < 2000:
        return None
    return image.content


def _wikidata_has_adult_occupation(client: object, qid: str) -> bool:
    import httpx

    assert isinstance(client, httpx.Client)
    try:
        response = client.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "claims",
                "format": "json",
            },
        )
    except httpx.HTTPError:
        return False
    if response.status_code != 200:
        return False
    try:
        entity = response.json()["entities"][qid]
    except (ValueError, KeyError, TypeError):
        return False
    claims = entity.get("claims") if isinstance(entity, dict) else None
    if not isinstance(claims, dict):
        return False
    for claim in claims.get("P106") or []:
        if not isinstance(claim, dict):
            continue
        mainsnak = claim.get("mainsnak") or {}
        datavalue = mainsnak.get("datavalue") if isinstance(mainsnak, dict) else None
        value = datavalue.get("value") if isinstance(datavalue, dict) else None
        if isinstance(value, dict) and value.get("id") in _ADULT_OCCUPATION_QIDS:
            return True
    return False


def _wikidata_image_filename(client: object, qid: str) -> str | None:
    import httpx

    assert isinstance(client, httpx.Client)
    try:
        response = client.get(
            "https://www.wikidata.org/w/api.php",
            params={
                "action": "wbgetentities",
                "ids": qid,
                "props": "claims",
                "format": "json",
            },
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        entity = response.json()["entities"][qid]
        claims = entity.get("claims") or {}
        p18 = claims.get("P18") or []
        mainsnak = p18[0]["mainsnak"]
        return str(mainsnak["datavalue"]["value"])
    except (ValueError, KeyError, TypeError, IndexError):
        return None


def _commons_thumb_bytes(client: object, filename: str) -> bytes | None:
    import httpx

    assert isinstance(client, httpx.Client)
    title = filename if filename.startswith("File:") else f"File:{filename}"
    try:
        response = client.get(
            "https://commons.wikimedia.org/w/api.php",
            params={
                "action": "query",
                "titles": title,
                "prop": "imageinfo",
                "iiprop": "url|mime|size",
                "iiurlwidth": 400,
                "format": "json",
            },
        )
    except httpx.HTTPError:
        return None
    if response.status_code != 200:
        return None
    try:
        pages = response.json()["query"]["pages"]
        info = next(iter(pages.values()))["imageinfo"][0]
    except (ValueError, KeyError, TypeError, StopIteration, IndexError):
        return None
    source = info.get("thumburl") or info.get("url")
    if not isinstance(source, str) or not source.startswith("https://"):
        return None
    try:
        image = client.get(source)
        image.raise_for_status()
    except httpx.HTTPError:
        return None
    if detect_image_ext(image.content) is None or len(image.content) < 2000:
        return None
    return image.content


# ---------------------------------------------------------------------------
# Legacy helpers kept for optional offline tooling / tests — NOT used by the
# default seed path or work-seed actor bootstrap.
# ---------------------------------------------------------------------------


def avatar_png(name: str, size: int = 256) -> bytes:
    """Deprecated: legacy identicon generator. Do not use in default seeding."""
    try:
        from PIL import Image, ImageDraw, ImageFilter, ImageFont
    except ImportError:
        return _avatar_png_raw(name, size)

    digest = hashlib.sha256(normalize_name(name).encode("utf-8")).digest()
    img = Image.new("RGB", (size, size))
    pixels = img.load()
    c1 = (28 + digest[0] % 40, 32 + digest[1] % 50, 48 + digest[2] % 70)
    c2 = (90 + digest[3] % 120, 70 + digest[4] % 110, 80 + digest[5] % 120)
    for y in range(size):
        t = y / (size - 1)
        red = int(c1[0] * (1 - t) + c2[0] * t)
        green = int(c1[1] * (1 - t) + c2[1] * t)
        blue = int(c1[2] * (1 - t) + c2[2] * t)
        for x in range(size):
            wobble = ((x * 17 + digest[6]) % 13) - 6
            pixels[x, y] = (
                max(0, min(255, red + wobble)),
                max(0, min(255, green + wobble // 2)),
                max(0, min(255, blue - wobble)),
            )

    overlay = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    draw = ImageDraw.Draw(overlay)
    grid = 5
    cell = size // (grid + 2)
    origin = (size - cell * grid) // 2
    accent = (digest[7], 40 + digest[8] % 80, 180 + digest[9] % 70, 210)
    light = (240, 246, 255, 200)
    for gy in range(grid):
        for gx in range((grid + 1) // 2):
            bit = digest[(gy * 3 + gx) % len(digest)]
            if bit % 3 == 0:
                continue
            color = accent if bit % 2 else light
            for col in {gx, grid - 1 - gx}:
                x0 = origin + col * cell + 3
                y0 = origin + gy * cell + 3
                box = (x0, y0, x0 + cell - 6, y0 + cell - 6)
                if bit % 5 == 0:
                    draw.ellipse(box, fill=color)
                else:
                    draw.rounded_rectangle(box, radius=8, fill=color)
    pad = size // 6
    draw.rounded_rectangle(
        (pad, pad, size - pad, size - pad),
        radius=size // 5,
        outline=(255, 255, 255, 70),
        width=3,
    )
    veil = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    ImageDraw.Draw(veil).ellipse(
        (size // 5, size // 5, size - size // 5, size - size // 5),
        fill=(12, 16, 28, 92),
    )
    composed = Image.alpha_composite(img.convert("RGBA"), overlay)
    composed = Image.alpha_composite(composed, veil.filter(ImageFilter.GaussianBlur(8)))
    img = composed.convert("RGB")
    text_draw = ImageDraw.Draw(img)
    initials = initials_for(name)
    font_size = 92 if len(initials) == 1 else 68
    try:
        font = ImageFont.truetype(str(FONT_PATH), font_size, index=3)
    except OSError:
        font = ImageFont.load_default()
    bbox = text_draw.textbbox((0, 0), initials, font=font)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    xy = ((size - tw) / 2 - bbox[0], (size - th) / 2 - bbox[1] - 4)
    text_draw.text((xy[0] + 2, xy[1] + 2), initials, font=font, fill=(20, 22, 32))
    text_draw.text(xy, initials, font=font, fill=(250, 252, 255))
    import io

    buf = io.BytesIO()
    img.save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _png_chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def _avatar_png_raw(name: str, size: int = 256) -> bytes:
    digest = hashlib.sha256(normalize_name(name).encode("utf-8")).digest()
    c1 = (28 + digest[0] % 40, 32 + digest[1] % 50, 48 + digest[2] % 70)
    c2 = (90 + digest[3] % 120, 70 + digest[4] % 110, 80 + digest[5] % 120)
    pixels: list[tuple[int, int, int]] = []
    grid = 5
    cell = size // (grid + 2)
    origin = (size - cell * grid) // 2
    for y in range(size):
        t = y / (size - 1)
        base = (
            int(c1[0] * (1 - t) + c2[0] * t),
            int(c1[1] * (1 - t) + c2[1] * t),
            int(c1[2] * (1 - t) + c2[2] * t),
        )
        for x in range(size):
            color = base
            gx = (x - origin) // cell
            gy = (y - origin) // cell
            if 0 <= gx < grid and 0 <= gy < grid:
                mx = min(gx, grid - 1 - gx)
                bit = digest[(gy * 3 + mx) % len(digest)]
                inset = 4
                inside = (
                    origin + gx * cell + inset <= x < origin + (gx + 1) * cell - inset
                    and origin + gy * cell + inset <= y < origin + (gy + 1) * cell - inset
                )
                if bit % 3 and inside:
                    color = (
                        (digest[7], 40 + digest[8] % 80, 180 + digest[9] % 70)
                        if bit % 2
                        else (240, 246, 255)
                    )
            pixels.append(color)
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            raw.extend(pixels[y * size + x])
    return (
        b"\x89PNG\r\n\x1a\n"
        + _png_chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
        + _png_chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _png_chunk(b"IEND", b"")
    )
