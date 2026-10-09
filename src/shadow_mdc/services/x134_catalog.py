"""134X (134x.com) long-form X/Twitter clip catalog — metadata only.

134X indexes X (Twitter) adult clips that are typically ≥1 hour. We treat it as
a *catalog discovery* source: extract JAV codes, poster URLs, durations, and
publisher handles. Stream / amplify video URLs are intentionally ignored so the
library stays catalog-first (no download, no embed playback dependency).
"""

from __future__ import annotations

import logging
import re
from datetime import UTC, datetime
from urllib.parse import urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field
from selectolax.parser import HTMLParser

from ..normalize_code import normalize_code, to_comparison_key
from ..providers.html import meta_content

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://134x.com"
LIST_PATHS: tuple[str, ...] = ("/popular", "/featured", "/")
BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

_VIDEO_HREF_RE = re.compile(r"/video/(\d{10,25})")
_CODE_RE = re.compile(r"\b([A-Za-z]{2,10})[-_ ](\d{2,5})\b")
_HANDLE_RE = re.compile(r"@\s*([A-Za-z0-9_]{2,30})")
_ISO8601_DURATION_RE = re.compile(
    r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?",
    re.IGNORECASE,
)
_TITLE_SUFFIX_RE = re.compile(r"\s*[｜|].*134X\s*$", re.IGNORECASE)  # noqa: RUF001


class X134ClipRef(BaseModel):
    """Lightweight listing row from popular/featured/home HTML."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_id: str
    list_name: str
    rank: int = Field(ge=1)
    href: str
    code_hints: tuple[str, ...] = ()
    handle_hint: str | None = None
    title_hint: str | None = None


class X134ClipDetail(BaseModel):
    """OG/meta snapshot from a /video/{id} page (no stream URLs)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    video_id: str
    source_url: str
    title: str
    description: str | None = None
    code: str | None = None
    x_handle: str | None = None
    duration_seconds: int | None = Field(default=None, ge=0)
    poster_url: str | None = None
    upload_date: str | None = None


class X134CatalogSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    fetched_at: str
    base_url: str = DEFAULT_BASE_URL
    lists: tuple[str, ...]
    refs: tuple[X134ClipRef, ...]
    details: tuple[X134ClipDetail, ...] = ()
    codes: tuple[str, ...] = ()
    handles: tuple[str, ...] = ()


def parse_list_html(
    html: str,
    *,
    list_name: str,
    base_url: str = DEFAULT_BASE_URL,
) -> tuple[X134ClipRef, ...]:
    """Extract ordered unique /video/{id} refs from a list page."""

    if not html or "Just a moment" in html[:2000]:
        return ()
    seen: set[str] = set()
    refs: list[X134ClipRef] = []
    # Prefer selectolax anchors; fall back to regex over raw HTML.
    root = HTMLParser(html)
    anchors = root.css('a[href*="/video/"]')
    ordered_ids: list[tuple[str, str]] = []
    for node in anchors:
        href = (node.attributes.get("href") or "").strip()
        match = _VIDEO_HREF_RE.search(href)
        if match is None:
            continue
        video_id = match.group(1)
        if video_id in seen:
            continue
        seen.add(video_id)
        absolute = urljoin(base_url.rstrip("/") + "/", href.lstrip("/"))
        ordered_ids.append((video_id, absolute))
    if not ordered_ids:
        seen.clear()
        for match in _VIDEO_HREF_RE.finditer(html):
            video_id = match.group(1)
            if video_id in seen:
                continue
            seen.add(video_id)
            ordered_ids.append((video_id, urljoin(base_url, f"/video/{video_id}")))

    for rank, (video_id, href) in enumerate(ordered_ids, start=1):
        # Local text window around the first occurrence for hints.
        window = _window_around(html, video_id, radius=500)
        code_hints = tuple(_extract_codes(window)[:3])
        handle = _first_handle(window)
        title_hint = _title_hint_from_window(window)
        refs.append(
            X134ClipRef(
                video_id=video_id,
                list_name=list_name,
                rank=rank,
                href=href,
                code_hints=code_hints,
                handle_hint=handle,
                title_hint=title_hint,
            )
        )
    return tuple(refs)


def parse_video_detail_html(
    html: str,
    *,
    video_id: str,
    base_url: str = DEFAULT_BASE_URL,
) -> X134ClipDetail:
    """Parse OG/meta from a video detail page. Ignores og:video stream URLs."""

    if not html or "Just a moment" in html[:2000]:
        raise ValueError(f"134x detail blocked or empty for video_id={video_id}")
    root = HTMLParser(html)
    title_raw = meta_content(root, "og:title") or meta_content(root, "twitter:title") or ""
    title = _TITLE_SUFFIX_RE.sub("", title_raw).strip() or title_raw.strip()
    if not title:
        title_node = root.css_first("title")
        title = (title_node.text(strip=True) if title_node else "") or f"134x:{video_id}"
        title = _TITLE_SUFFIX_RE.sub("", title).strip()
    description = meta_content(root, "og:description") or meta_content(root, "description")
    poster = meta_content(root, "og:image") or meta_content(root, "twitter:image")
    # Prefer numeric video:duration; never read og:video.
    duration_seconds = _parse_duration_seconds(
        meta_content(root, "video:duration"),
        description,
        html,
    )
    blob = " ".join(part for part in (title, description or "") if part)
    codes = _extract_codes(blob)
    code = codes[0] if codes else None
    handle = _first_handle(description or "") or _first_handle(blob)
    upload_date = _extract_upload_date(html)
    source_url = meta_content(root, "og:url") or urljoin(base_url, f"/video/{video_id}")
    return X134ClipDetail(
        video_id=video_id,
        source_url=source_url,
        title=title,
        description=description,
        code=code,
        x_handle=handle,
        duration_seconds=duration_seconds,
        poster_url=poster if poster and poster.startswith("http") else None,
        upload_date=upload_date,
    )


def merge_refs(*groups: tuple[X134ClipRef, ...]) -> tuple[X134ClipRef, ...]:
    """Dedupe by video_id, keeping the first (highest-priority list) occurrence."""

    seen: set[str] = set()
    out: list[X134ClipRef] = []
    for group in groups:
        for ref in group:
            if ref.video_id in seen:
                continue
            seen.add(ref.video_id)
            out.append(ref)
    return tuple(out)


def codes_from_snapshot(snapshot: X134CatalogSnapshot) -> tuple[str, ...]:
    """Prefer detail codes, then list hints; stable unique order."""

    ordered: list[str] = []
    seen: set[str] = set()
    for detail in snapshot.details:
        if detail.code:
            key = to_comparison_key(detail.code)
            if key and key not in seen:
                seen.add(key)
                ordered.append(normalize_code(detail.code))
    for ref in snapshot.refs:
        for hint in ref.code_hints:
            key = to_comparison_key(hint)
            if key and key not in seen:
                seen.add(key)
                ordered.append(normalize_code(hint))
    return tuple(ordered)


def handles_from_snapshot(snapshot: X134CatalogSnapshot) -> tuple[str, ...]:
    ordered: list[str] = []
    seen: set[str] = set()
    for detail in snapshot.details:
        if detail.x_handle:
            key = detail.x_handle.casefold()
            if key not in seen:
                seen.add(key)
                ordered.append(detail.x_handle)
    for ref in snapshot.refs:
        if ref.handle_hint:
            key = ref.handle_hint.casefold()
            if key not in seen:
                seen.add(key)
                ordered.append(ref.handle_hint)
    return tuple(ordered)


async def fetch_list_html(
    client: httpx.AsyncClient,
    path: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> str:
    url = urljoin(base_url.rstrip("/") + "/", path.lstrip("/"))
    response = await client.get(
        url,
        headers={
            "User-Agent": BROWSER_UA,
            "Accept": "text/html,application/xhtml+xml",
            "Accept-Language": "zh-TW,zh;q=0.9,en;q=0.7",
        },
    )
    response.raise_for_status()
    return response.text


async def fetch_video_detail(
    client: httpx.AsyncClient,
    video_id: str,
    *,
    base_url: str = DEFAULT_BASE_URL,
) -> X134ClipDetail:
    html = await fetch_list_html(client, f"/video/{video_id}", base_url=base_url)
    return parse_video_detail_html(html, video_id=video_id, base_url=base_url)


async def build_catalog_snapshot(
    client: httpx.AsyncClient,
    *,
    base_url: str = DEFAULT_BASE_URL,
    list_paths: tuple[str, ...] = LIST_PATHS,
    detail_limit: int = 25,
) -> X134CatalogSnapshot:
    """Fetch list pages, optionally enrich top refs with detail OG metadata."""

    groups: list[tuple[X134ClipRef, ...]] = []
    used_lists: list[str] = []
    for path in list_paths:
        list_name = path.strip("/") or "home"
        try:
            html = await fetch_list_html(client, path, base_url=base_url)
        except httpx.HTTPError as exc:
            logger.warning("134x list fetch failed path=%s: %s", path, exc)
            continue
        refs = parse_list_html(html, list_name=list_name, base_url=base_url)
        if refs:
            used_lists.append(list_name)
            groups.append(refs)
    merged = merge_refs(*groups)
    details: list[X134ClipDetail] = []
    for ref in merged[: max(0, detail_limit)]:
        try:
            details.append(await fetch_video_detail(client, ref.video_id, base_url=base_url))
        except (httpx.HTTPError, ValueError) as exc:
            logger.warning("134x detail fetch failed id=%s: %s", ref.video_id, exc)
    snapshot = X134CatalogSnapshot(
        fetched_at=datetime.now(UTC).isoformat(),
        base_url=base_url,
        lists=tuple(used_lists),
        refs=merged,
        details=tuple(details),
    )
    return snapshot.model_copy(
        update={
            "codes": codes_from_snapshot(snapshot),
            "handles": handles_from_snapshot(snapshot),
        }
    )


def _window_around(html: str, video_id: str, *, radius: int) -> str:
    needle = f"/video/{video_id}"
    idx = html.find(needle)
    if idx < 0:
        return ""
    return html[max(0, idx - radius) : idx + radius]


def _extract_codes(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    for match in _CODE_RE.finditer(text):
        raw = f"{match.group(1)}-{match.group(2)}"
        normalized = normalize_code(raw)
        key = to_comparison_key(normalized)
        # Filter obvious non-codes (e.g. HTML/CSS tokens rarely match this shape).
        if not key or len(match.group(1)) < 2:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(normalized)
    return found


def _first_handle(text: str) -> str | None:
    match = _HANDLE_RE.search(text or "")
    return match.group(1) if match else None


def _title_hint_from_window(window: str) -> str | None:
    if not window:
        return None
    # Strip tags lightly.
    text = re.sub(r"<[^>]+>", " ", window)
    text = re.sub(r"\s+", " ", text).strip()
    codes = _extract_codes(text)
    if codes:
        # Prefer "CODE rest…" snippet
        match = re.search(re.escape(codes[0]) + r"\s+(.{0,60})", text)
        if match:
            return f"{codes[0]} {match.group(1).strip()}"[:80]
        return codes[0]
    return text[:80] if text else None


def _parse_duration_seconds(*candidates: str | None) -> int | None:
    for raw in candidates:
        if not raw:
            continue
        stripped = raw.strip()
        if stripped.isdigit():
            value = int(stripped)
            if value > 0:
                return value
        iso = _ISO8601_DURATION_RE.search(stripped)
        if iso:
            hours = int(iso.group(1) or 0)
            minutes = int(iso.group(2) or 0)
            seconds = int(iso.group(3) or 0)
            total = hours * 3600 + minutes * 60 + seconds
            if total > 0:
                return total
        # Chinese prose: 時長 1 小時 57 分鐘
        zh = re.search(
            r"(\d+)\s*小時\s*(\d+)\s*分鐘|(\d+)\s*小时\s*(\d+)\s*分钟",
            stripped,
        )
        if zh:
            h = int(zh.group(1) or zh.group(3) or 0)
            m = int(zh.group(2) or zh.group(4) or 0)
            total = h * 3600 + m * 60
            if total > 0:
                return total
    return None


def _extract_upload_date(html: str) -> str | None:
    match = re.search(r'"uploadDate"\s*:\s*"([^"]+)"', html)
    return match.group(1) if match else None

