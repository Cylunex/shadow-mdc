"""Identicon avatars and optional public Wikipedia portraits for non-JAV actors."""

from __future__ import annotations

import hashlib
import io
import struct
import unicodedata
import zlib
from pathlib import Path

FONT_PATH = Path("/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc")


def normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


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


def avatar_png(name: str, size: int = 256) -> bytes:
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
                    color = (digest[7], 40 + digest[8] % 80, 180 + digest[9] % 70) if bit % 2 else (240, 246, 255)
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


_WIKI_MARKERS = ("adult film", "pornographic", "porn star", "erotic film", "xxx", "onlyfans")


def wikipedia_photo(name: str, aliases: list[str]) -> bytes | None:
    try:
        import httpx
    except ImportError:
        return None
    headers = {
        "User-Agent": "ShadowMDC/0.1 (https://github.com; non-jav-avatar-seed)",
        "Accept": "application/json",
    }
    with httpx.Client(timeout=12.0, follow_redirects=True, headers=headers) as client:
        for title in [name, *aliases]:
            slug = title.replace(" ", "_")
            try:
                response = client.get(f"https://en.wikipedia.org/api/rest_v1/page/summary/{slug}")
            except httpx.HTTPError:
                continue
            if response.status_code != 200:
                continue
            try:
                payload = response.json()
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            extract = str(payload.get("extract") or payload.get("description") or "").casefold()
            if not any(marker in extract for marker in _WIKI_MARKERS):
                continue
            thumb = payload.get("thumbnail") or payload.get("originalimage") or {}
            source = thumb.get("source") if isinstance(thumb, dict) else None
            if not isinstance(source, str) or not source.startswith("https://upload.wikimedia.org/"):
                continue
            try:
                image = client.get(source)
                image.raise_for_status()
            except httpx.HTTPError:
                continue
            if detect_image_ext(image.content) is None or len(image.content) < 2000:
                continue
            return image.content
    return None
