"""Online / Emby playback resolve for works.

Ideas only (no code copy): jav-play-style external deep links by code;
shadow-relay resolve→JSON (Emby/Jellyfin stay direct-client); Apple-CMS VOD
search → m3u8 with adult category whitelist / ban underage-theme type names.

LumenBox-inspired hardening (rewritten, not copied):
- bounded multi-source resolve with a total timeout budget and ordered
  failover (Emby → external templates → VOD); probe failures skip to next;
- m3u8 integrity gate before treating a VOD/HLS URL as playable (require
  ``#EXTM3U`` plus a segment or master variant; truncated / empty / HTML
  error pages fail and are not cached as success).

Site hostnames and API bases live in a **local** JSON file (never committed):
``SHADOW_MDC_PLAYBACK_SITES_PATH`` or ``{data_dir}/playback-sites.local.json``.
This module only knows the schema. Emby deep links use media-server settings.
"""

from __future__ import annotations

import json
import logging
import re
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote, urlencode

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .media_server import MediaServerSettings, build_media_server_deep_link

logger = logging.getLogger(__name__)

PlayKind = Literal["emby", "jellyfin", "external_site", "vod_m3u8"]

# type_name fragments that must never be collected (standing boss rules).
_BANNED_TYPE_FRAGMENTS = (
    "萝莉",
    "童颜",
    "幼女",
    "儿童",
    "小学生",
    "中学生",
    "学生妹",
    "学生校园",
    "可爱学生",
    "教师学生",
    "少女萝莉",
    "极骚萝莉",
    "淫乱学生",
    "李宗瑞",
    "未成年",
    "幼齿",
    "正太",
    "JC",
    "JS",
    "loin",
    "lolita",
    "loli",
)

# Default integrity-gate bounds (overridable via PlaybackSitesFile).
_DEFAULT_RESOLVE_BUDGET_MS = 12_000
_DEFAULT_M3U8_PROBE_TIMEOUT_S = 6.5
_DEFAULT_M3U8_MAX_BYTES = 768 * 1024
_PROBE_CACHE_MAX = 64
_PROBE_CACHE_TTL_S = 120.0


class PlaySource(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: PlayKind
    label: str
    url: str
    playable_in_app: bool = False
    app_scheme: str | None = None
    item_id: str | None = None
    provider: str | None = None
    note: str | None = None
    probe_ok: bool | None = None
    probe_reason: str | None = None


class PlayProbeFailure(BaseModel):
    """Brief structured failure from one resolve / probe attempt (no secrets)."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    stage: str  # emby | vod_search | m3u8_probe | budget
    provider: str | None = None
    reason: str
    detail: str | None = None
    elapsed_ms: int | None = None


class PlayResolveResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    code: str | None = None
    preferred: PlaySource | None = None
    sources: list[PlaySource] = Field(default_factory=list)
    emby_in_library: bool = False
    chosen: PlaySource | None = None
    failures: list[PlayProbeFailure] = Field(default_factory=list)
    resolve_ms: int = 0


class ExternalSiteTemplate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    label: str
    url_template: str  # {code} / {code_lower} / {code_upper}
    enabled: bool = True


class VodProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    base_url: str
    whitelist_type_ids: list[int] = Field(default_factory=list)
    enabled: bool = True
    timeout_seconds: float = Field(default=8.0, ge=1.0, le=60.0)
    # Optional override for m3u8 integrity probe (falls back to file-level default).
    m3u8_probe_timeout_seconds: float | None = Field(default=None, ge=0.5, le=30.0)


class PlaybackSitesFile(BaseModel):
    """Schema of the local (gitignored) sites file."""

    model_config = ConfigDict(extra="ignore")

    version: int = 1
    external_sites: list[ExternalSiteTemplate] = Field(default_factory=list)
    vod_providers: list[VodProviderConfig] = Field(default_factory=list)
    vod_max_providers: int = Field(default=4, ge=0, le=20)
    vod_max_sources: int = Field(default=8, ge=0, le=50)
    # Bounded multi-source resolve (LumenBox-inspired budget; rewritten).
    resolve_budget_ms: int = Field(default=_DEFAULT_RESOLVE_BUDGET_MS, ge=500, le=120_000)
    m3u8_probe_enabled: bool = True
    m3u8_probe_timeout_seconds: float = Field(
        default=_DEFAULT_M3U8_PROBE_TIMEOUT_S, ge=0.5, le=30.0
    )
    m3u8_max_bytes: int = Field(default=_DEFAULT_M3U8_MAX_BYTES, ge=1024, le=2_000_000)


class PlaybackSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    # Absolute/relative path to PlaybackSitesFile JSON. Empty → data_dir default.
    sites_path: str | None = None
    # Optional inline overrides (tests / emergency). Prefer the sites file.
    sites_json: str = ""


def load_sites_file(path: Path | None) -> PlaybackSitesFile:
    if path is None:
        return PlaybackSitesFile()
    try:
        if not path.is_file():
            return PlaybackSitesFile()
        text = path.read_text(encoding="utf-8-sig")
        if not text.strip():
            return PlaybackSitesFile()
        return PlaybackSitesFile.model_validate_json(text)
    except (OSError, UnicodeError, json.JSONDecodeError, ValidationError, ValueError) as exc:
        logger.warning("playback: cannot load sites file %s: %s", path, type(exc).__name__)
        return PlaybackSitesFile()


def load_sites_from_settings(
    settings: PlaybackSettings,
    *,
    data_dir: Path | None = None,
) -> PlaybackSitesFile:
    if settings.sites_json.strip():
        try:
            return PlaybackSitesFile.model_validate_json(settings.sites_json)
        except (ValidationError, ValueError, json.JSONDecodeError):
            logger.warning("playback: invalid sites_json override")
    path: Path | None = None
    if settings.sites_path:
        path = Path(settings.sites_path)
    elif data_dir is not None:
        path = Path(data_dir) / "playback-sites.local.json"
    return load_sites_file(path)


def expand_site_url(template: str, code: str) -> str:
    c = code.strip()
    return (
        template.replace("{code_lower}", c.lower())
        .replace("{code_upper}", c.upper())
        .replace("{code}", c)
    )


def type_name_banned(type_name: str | None) -> bool:
    if not type_name:
        return False
    name = str(type_name)
    lower = name.lower()
    for frag in _BANNED_TYPE_FRAGMENTS:
        if frag.isascii():
            if frag.lower() in lower:
                return True
        elif frag in name:
            return True
    return False


def build_item_deep_link(
    settings: MediaServerSettings,
    *,
    item_id: str,
    server_id: str | None = None,
) -> str | None:
    if not settings.base_url:
        return None
    base = settings.base_url.rstrip("/")
    if settings.kind == "emby":
        q = {"id": item_id, "context": "item"}
        if server_id:
            q["serverId"] = server_id
        return f"{base}/web/index.html#!/item?{urlencode(q)}"
    return f"{base}/web/index.html#!/details?id={quote(item_id)}"


def build_app_scheme(settings: MediaServerSettings, *, item_id: str) -> str | None:
    if settings.kind == "emby":
        return f"emby://items/{item_id}"
    return f"jellyfin://items/{item_id}"


def _score_item(item: dict[str, Any], code: str) -> int:
    needle = code.strip().upper().replace("-", "")
    if not needle:
        return 0
    name = str(item.get("Name") or "")
    path = str(item.get("Path") or "")
    folded_name = name.upper().replace("-", "").replace(" ", "")
    folded_path = path.upper().replace("-", "").replace(" ", "")
    score = 0
    if needle == folded_name or folded_name.startswith(needle) or needle in folded_name:
        score += 100
    if needle in folded_path:
        score += 40
    itype = str(item.get("Type") or "")
    if itype in {"Movie", "Video", "Episode"}:
        score += 10
    return score


async def resolve_emby_item(
    settings: MediaServerSettings,
    client: httpx.AsyncClient,
    *,
    code: str,
    timeout: float = 12.0,
) -> dict[str, Any] | None:
    if not settings.enabled or not settings.base_url or not settings.api_key:
        return None
    q = code.strip()
    if not q:
        return None
    base = settings.base_url.rstrip("/")
    headers = {"X-Emby-Token": settings.api_key}
    try:
        response = await client.get(
            f"{base}/Items",
            headers=headers,
            params={
                "SearchTerm": q,
                "Recursive": "true",
                "IncludeItemTypes": "Movie,Video,Episode",
                "Fields": "Path,Overview,ServerId",
                "Limit": "15",
            },
            timeout=timeout,
        )
        if response.status_code >= 400:
            logger.info("playback: media-server Items HTTP %s", response.status_code)
            return None
        payload = response.json()
    except (httpx.HTTPError, ValueError, TypeError) as exc:
        logger.info("playback: media-server Items failed: %s", type(exc).__name__)
        return None
    items = payload.get("Items") if isinstance(payload, dict) else None
    if not isinstance(items, list) or not items:
        return None
    ranked: list[tuple[int, dict[str, Any]]] = []
    for raw in items:
        if not isinstance(raw, dict) or not raw.get("Id"):
            continue
        ranked.append((_score_item(raw, q), raw))
    ranked.sort(key=lambda pair: pair[0], reverse=True)
    if not ranked or ranked[0][0] <= 0:
        return None
    return ranked[0][1]


def extract_m3u8_urls(vod_play_url: str | None) -> list[str]:
    """Parse Apple-CMS vod_play_url ($$$ / # / $ separated) for http(s) m3u8 links."""

    if not vod_play_url:
        return []
    found: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[#\$]+", vod_play_url):
        part = token.strip()
        if not part:
            continue
        if "$" in part:
            part = part.split("$")[-1].strip()
        if not part.startswith("http"):
            continue
        if ".m3u8" not in part.lower():
            continue
        if part in seen:
            continue
        seen.add(part)
        found.append(part)
    return found


# ---------------------------------------------------------------------------
# m3u8 integrity gate (ideas from LumenBox playlist capture; rewritten)
# ---------------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class M3u8ProbeResult:
    ok: bool
    reason: str
    status_code: int | None = None
    bytes_read: int = 0
    elapsed_ms: int = 0


def validate_m3u8_playlist_text(text: str) -> str | None:
    """Return ``None`` if the body looks like a usable HLS playlist, else a reason.

    Requires a leading ``#EXTM3U`` (BOM/whitespace stripped) and either a media
    segment (``#EXTINF``) or a master variant (``#EXT-X-STREAM-INF``). Empty,
    HTML error pages, and bodies missing both markers fail.
    """

    if not text:
        return "empty"
    stripped = text.lstrip("\ufeff").lstrip()
    if not stripped:
        return "empty"
    head = stripped[:64].lower()
    if head.startswith("<!doctype") or head.startswith("<html") or head.startswith("<head"):
        return "html"
    if not stripped.startswith("#EXTM3U"):
        return "not_m3u8"
    has_segment = "#EXTINF" in stripped
    has_variant = "#EXT-X-STREAM-INF" in stripped
    if not has_segment and not has_variant:
        return "no_segments"
    return None


class _M3u8ProbeCache:
    """Small TTL LRU for probe outcomes (success and hard failures). Truncation
    / empty / HTML are stored as failures so the player does not retry them as
    success; callers must not treat a cache miss as ok.
    """

    def __init__(self, *, max_entries: int = _PROBE_CACHE_MAX, ttl_s: float = _PROBE_CACHE_TTL_S) -> None:
        self._max = max_entries
        self._ttl = ttl_s
        self._entries: OrderedDict[str, tuple[float, M3u8ProbeResult]] = OrderedDict()

    def get(self, url: str) -> M3u8ProbeResult | None:
        hit = self._entries.get(url)
        if hit is None:
            return None
        ts, result = hit
        if time.monotonic() - ts > self._ttl:
            self._entries.pop(url, None)
            return None
        self._entries.move_to_end(url)
        return result

    def put(self, url: str, result: M3u8ProbeResult) -> None:
        self._entries[url] = (time.monotonic(), result)
        self._entries.move_to_end(url)
        while len(self._entries) > self._max:
            self._entries.popitem(last=False)

    def clear(self) -> None:
        self._entries.clear()


_PROBE_CACHE = _M3u8ProbeCache()


def clear_m3u8_probe_cache() -> None:
    """Test helper: drop in-process probe cache."""

    _PROBE_CACHE.clear()


async def probe_m3u8_url(
    client: httpx.AsyncClient,
    url: str,
    *,
    timeout_seconds: float = _DEFAULT_M3U8_PROBE_TIMEOUT_S,
    max_bytes: int = _DEFAULT_M3U8_MAX_BYTES,
    use_cache: bool = True,
) -> M3u8ProbeResult:
    """Fetch a playlist with bounded size/time; never treat truncated bodies as ok.

    Overflow past ``max_bytes`` → ``truncated`` (not cached as success). Non-200,
    empty, HTML, or missing ``#EXTM3U``/segments → fail reasons. Reused by
    ``resolve_play`` and suitable for an in-app player preflight.
    """

    if use_cache:
        cached = _PROBE_CACHE.get(url)
        if cached is not None:
            return cached

    started = time.monotonic()
    try:
        async with client.stream(
            "GET",
            url,
            timeout=timeout_seconds,
            follow_redirects=True,
            headers={"Accept": "application/vnd.apple.mpegurl,application/x-mpegURL,audio/mpegurl,text/plain,*/*"},
        ) as response:
            status = response.status_code
            if status >= 400:
                result = M3u8ProbeResult(
                    ok=False,
                    reason=f"http_{status}",
                    status_code=status,
                    elapsed_ms=int((time.monotonic() - started) * 1000),
                )
                if use_cache:
                    _PROBE_CACHE.put(url, result)
                return result
            buf = bytearray()
            truncated = False
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                remaining = max_bytes - len(buf)
                if remaining <= 0:
                    truncated = True
                    break
                if len(chunk) > remaining:
                    buf.extend(chunk[:remaining])
                    truncated = True
                    break
                buf.extend(chunk)
            elapsed_ms = int((time.monotonic() - started) * 1000)
            if truncated:
                # Discard — truncated playlists must not pollute keys / cache-as-ok.
                result = M3u8ProbeResult(
                    ok=False,
                    reason="truncated",
                    status_code=status,
                    bytes_read=len(buf),
                    elapsed_ms=elapsed_ms,
                )
                if use_cache:
                    _PROBE_CACHE.put(url, result)
                return result
            if not buf:
                result = M3u8ProbeResult(
                    ok=False,
                    reason="empty",
                    status_code=status,
                    bytes_read=0,
                    elapsed_ms=elapsed_ms,
                )
                if use_cache:
                    _PROBE_CACHE.put(url, result)
                return result
            try:
                text = buf.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                result = M3u8ProbeResult(
                    ok=False,
                    reason="decode_error",
                    status_code=status,
                    bytes_read=len(buf),
                    elapsed_ms=elapsed_ms,
                )
                if use_cache:
                    _PROBE_CACHE.put(url, result)
                return result
            reason = validate_m3u8_playlist_text(text)
            if reason is not None:
                result = M3u8ProbeResult(
                    ok=False,
                    reason=reason,
                    status_code=status,
                    bytes_read=len(buf),
                    elapsed_ms=elapsed_ms,
                )
                if use_cache:
                    _PROBE_CACHE.put(url, result)
                return result
            result = M3u8ProbeResult(
                ok=True,
                reason="ok",
                status_code=status,
                bytes_read=len(buf),
                elapsed_ms=elapsed_ms,
            )
            if use_cache:
                _PROBE_CACHE.put(url, result)
            return result
    except httpx.TimeoutException:
        result = M3u8ProbeResult(
            ok=False,
            reason="timeout",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        if use_cache:
            _PROBE_CACHE.put(url, result)
        return result
    except httpx.HTTPError as exc:
        result = M3u8ProbeResult(
            ok=False,
            reason=f"http_error:{type(exc).__name__}",
            elapsed_ms=int((time.monotonic() - started) * 1000),
        )
        if use_cache:
            _PROBE_CACHE.put(url, result)
        return result


async def search_vod_provider(
    provider: VodProviderConfig,
    client: httpx.AsyncClient,
    *,
    code: str,
    timeout_seconds: float | None = None,
) -> list[PlaySource]:
    if not provider.enabled:
        return []
    base = provider.base_url.rstrip("/")
    raw = code.strip()
    # Some Apple-CMS adult indexes match "SSIS001" but not "SSIS-001".
    queries: list[str] = []
    for q in (raw, raw.replace("-", ""), raw.upper(), raw.upper().replace("-", "")):
        if q and q not in queries:
            queries.append(q)
    rows: list[Any] = []
    timeout = timeout_seconds if timeout_seconds is not None else provider.timeout_seconds
    for wd in queries:
        try:
            response = await client.get(
                base,
                params={"ac": "detail", "wd": wd},
                timeout=timeout,
            )
            if response.status_code >= 400:
                continue
            payload = response.json()
        except (httpx.HTTPError, ValueError, TypeError):
            continue
        if not isinstance(payload, dict):
            continue
        candidate = payload.get("list")
        if isinstance(candidate, list) and candidate:
            rows = candidate
            break
    if not rows:
        return []
    whitelist = set(provider.whitelist_type_ids)
    sources: list[PlaySource] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        type_id = row.get("type_id")
        try:
            tid = int(type_id) if type_id is not None else None
        except (TypeError, ValueError):
            tid = None
        type_name = row.get("type_name")
        if type_name_banned(str(type_name) if type_name is not None else None):
            continue
        if whitelist and (tid is None or tid not in whitelist):
            continue
        title = str(row.get("vod_name") or "")
        for url in extract_m3u8_urls(
            str(row.get("vod_play_url") or "") if row.get("vod_play_url") else None
        ):
            sources.append(
                PlaySource(
                    kind="vod_m3u8",
                    label=f"{provider.name}: {title[:60] or code}",
                    url=url,
                    playable_in_app=True,
                    provider=provider.name,
                )
            )
    return sources


def external_site_sources(sites: PlaybackSitesFile, code: str | None) -> list[PlaySource]:
    if not code or not code.strip():
        return []
    out: list[PlaySource] = []
    for site in sites.external_sites:
        if not site.enabled:
            continue
        out.append(
            PlaySource(
                kind="external_site",
                label=site.label,
                url=expand_site_url(site.url_template, code.strip()),
                playable_in_app=False,
                provider=site.label,
                note="external site (local sites file)",
            )
        )
    return out


def _remaining_seconds(deadline: float) -> float:
    return max(0.0, deadline - time.monotonic())


def _pick_preferred(sources: list[PlaySource], emby_preferred: PlaySource | None) -> PlaySource | None:
    if emby_preferred is not None:
        return emby_preferred
    preferred = next((s for s in sources if s.kind in {"emby", "jellyfin"} and s.item_id), None)
    if preferred is None:
        preferred = next((s for s in sources if s.playable_in_app and s.probe_ok is not False), None)
    if preferred is None:
        preferred = next((s for s in sources if s.playable_in_app), None)
    if preferred is None:
        preferred = sources[0] if sources else None
    return preferred


async def resolve_play(
    *,
    work_id: str,
    code: str | None,
    title: str | None,
    media: MediaServerSettings,
    sites: PlaybackSitesFile,
    client: httpx.AsyncClient,
    vod_client: httpx.AsyncClient | None = None,
) -> PlayResolveResult:
    """Resolve play sources with a total budget and ordered failover.

    Order: Emby/Jellyfin library hit → external site templates → VOD m3u8
    (search then integrity probe). Probe failures are skipped and recorded in
    ``failures``; already-found sources are still returned when the budget
    runs out.
    """

    started = time.monotonic()
    deadline = started + (sites.resolve_budget_ms / 1000.0)
    sources: list[PlaySource] = []
    failures: list[PlayProbeFailure] = []
    emby_in_library = False
    emby_preferred: PlaySource | None = None

    query = (code or title or "").strip()
    if media.enabled and query and _remaining_seconds(deadline) > 0.2:
        emby_timeout = min(12.0, max(1.0, _remaining_seconds(deadline)))
        t0 = time.monotonic()
        item = await resolve_emby_item(
            media, client, code=code or query, timeout=emby_timeout
        )
        elapsed = int((time.monotonic() - t0) * 1000)
        if item is not None:
            item_id = str(item["Id"])
            server_id = str(item["ServerId"]) if item.get("ServerId") else None
            deep = build_item_deep_link(media, item_id=item_id, server_id=server_id)
            if deep:
                emby_in_library = True
                kind: PlayKind = "emby" if media.kind == "emby" else "jellyfin"
                emby_preferred = PlaySource(
                    kind=kind,
                    label=f"{media.kind.title()} · {item.get('Name') or code or item_id}",
                    url=deep,
                    playable_in_app=False,
                    app_scheme=build_app_scheme(media, item_id=item_id),
                    item_id=item_id,
                    provider=media.kind,
                    note="in library — open in media server",
                    probe_ok=True,
                    probe_reason="ok",
                )
                sources.append(emby_preferred)
        else:
            failures.append(
                PlayProbeFailure(
                    stage="emby",
                    provider=media.kind,
                    reason="no_match",
                    detail="Items search miss or disabled",
                    elapsed_ms=elapsed,
                )
            )
            search_url = build_media_server_deep_link(media, query)
            if search_url:
                kind = "emby" if media.kind == "emby" else "jellyfin"
                sources.append(
                    PlaySource(
                        kind=kind,
                        label=f"{media.kind.title()} 搜索",
                        url=search_url,
                        playable_in_app=False,
                        provider=media.kind,
                        note="not confirmed in library — search page",
                        probe_ok=None,
                    )
                )

    # External templates are cheap (no HTTP); always attach when code present.
    sources.extend(external_site_sources(sites, code))

    providers = [p for p in sites.vod_providers if p.enabled]
    vod_cap = sites.vod_max_providers
    source_cap = sites.vod_max_sources
    if vod_cap > 0 and source_cap > 0 and code and _remaining_seconds(deadline) > 0.3:
        # VOD collect APIs are usually reachable direct from NAS; the app proxy
        # (for JavDB etc.) often slows or empties them — use a short-lived direct
        # client unless the caller injects one (tests / shared pool).
        owns_vod_client = vod_client is None
        active_vod = vod_client or httpx.AsyncClient(
            follow_redirects=True,
            timeout=httpx.Timeout(12.0),
            headers={"User-Agent": "ShadowMDC/0.1"},
            proxy=None,
            trust_env=False,
        )
        try:
            vod_added = 0
            for provider in providers[:vod_cap]:
                remaining = _remaining_seconds(deadline)
                if remaining < 0.4:
                    failures.append(
                        PlayProbeFailure(
                            stage="budget",
                            provider=provider.name,
                            reason="budget_exhausted",
                            detail="resolve budget depleted before provider",
                        )
                    )
                    break
                search_timeout = min(provider.timeout_seconds, max(0.5, remaining - 0.2))
                t0 = time.monotonic()
                try:
                    candidates = await search_vod_provider(
                        provider,
                        active_vod,
                        code=code,
                        timeout_seconds=search_timeout,
                    )
                except Exception as exc:  # noqa: BLE001 — isolate flaky upstreams
                    logger.info("playback: vod provider %s failed", provider.name)
                    failures.append(
                        PlayProbeFailure(
                            stage="vod_search",
                            provider=provider.name,
                            reason="exception",
                            detail=type(exc).__name__,
                            elapsed_ms=int((time.monotonic() - t0) * 1000),
                        )
                    )
                    continue
                search_ms = int((time.monotonic() - t0) * 1000)
                if not candidates:
                    failures.append(
                        PlayProbeFailure(
                            stage="vod_search",
                            provider=provider.name,
                            reason="no_match",
                            elapsed_ms=search_ms,
                        )
                    )
                    continue
                seen_urls = {s.url for s in sources if s.kind == "vod_m3u8"}
                for cand in candidates:
                    if vod_added >= source_cap:
                        break
                    if cand.url in seen_urls:
                        continue
                    remaining = _remaining_seconds(deadline)
                    if remaining < 0.3:
                        failures.append(
                            PlayProbeFailure(
                                stage="budget",
                                provider=provider.name,
                                reason="budget_exhausted",
                                detail="resolve budget depleted during m3u8 probe",
                            )
                        )
                        break
                    if sites.m3u8_probe_enabled:
                        provider_probe = (
                            provider.m3u8_probe_timeout_seconds
                            if provider.m3u8_probe_timeout_seconds is not None
                            else sites.m3u8_probe_timeout_seconds
                        )
                        probe_timeout = min(
                            provider_probe,
                            max(0.4, remaining - 0.1),
                        )
                        probe = await probe_m3u8_url(
                            active_vod,
                            cand.url,
                            timeout_seconds=probe_timeout,
                            max_bytes=sites.m3u8_max_bytes,
                        )
                        if not probe.ok:
                            failures.append(
                                PlayProbeFailure(
                                    stage="m3u8_probe",
                                    provider=provider.name,
                                    reason=probe.reason,
                                    detail=f"status={probe.status_code}",
                                    elapsed_ms=probe.elapsed_ms,
                                )
                            )
                            continue
                        gated = PlaySource(
                            kind=cand.kind,
                            label=cand.label,
                            url=cand.url,
                            playable_in_app=True,
                            provider=cand.provider,
                            note=cand.note,
                            probe_ok=True,
                            probe_reason="ok",
                        )
                    else:
                        gated = PlaySource(
                            kind=cand.kind,
                            label=cand.label,
                            url=cand.url,
                            playable_in_app=True,
                            provider=cand.provider,
                            note=cand.note,
                            probe_ok=None,
                            probe_reason="probe_skipped",
                        )
                    seen_urls.add(gated.url)
                    sources.append(gated)
                    vod_added += 1
                if vod_added >= source_cap:
                    break
                if _remaining_seconds(deadline) < 0.3:
                    break
        finally:
            if owns_vod_client:
                await active_vod.aclose()

    preferred = _pick_preferred(sources, emby_preferred)
    resolve_ms = int((time.monotonic() - started) * 1000)
    return PlayResolveResult(
        work_id=work_id,
        code=code,
        preferred=preferred,
        sources=sources,
        emby_in_library=emby_in_library,
        chosen=preferred,
        failures=failures,
        resolve_ms=resolve_ms,
    )


PLAYER_HTML = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>播放 · Shadow MDC</title>
<style>
  :root { color-scheme: dark; --bg:#0f1115; --fg:#e8eaed; --muted:#9aa0a6; --accent:#5b8cff; --card:#1a1d24; }
  * { box-sizing: border-box; }
  body { margin:0; font-family: system-ui, sans-serif; background:var(--bg); color:var(--fg); }
  header { display:flex; gap:12px; align-items:center; padding:12px 16px; border-bottom:1px solid #2a2f3a; }
  header a { color:var(--accent); text-decoration:none; }
  h1 { font-size:16px; margin:0; font-weight:600; }
  main { display:grid; grid-template-columns: 1fr 280px; gap:16px; padding:16px; max-width:1200px; margin:0 auto; }
  @media (max-width: 800px) { main { grid-template-columns: 1fr; } }
  .stage { background:#000; border-radius:10px; overflow:hidden; aspect-ratio:16/9; }
  video { width:100%; height:100%; background:#000; }
  .sources { background:var(--card); border-radius:10px; padding:12px; max-height:70vh; overflow:auto; }
  .sources h2 { margin:0 0 8px; font-size:13px; color:var(--muted); font-weight:600; }
  .src { display:block; width:100%; text-align:left; margin:0 0 6px; padding:8px 10px; border:1px solid #2a2f3a;
         border-radius:8px; background:transparent; color:var(--fg); cursor:pointer; font-size:13px; }
  .src.active, .src:hover { border-color:var(--accent); }
  .src small { display:block; color:var(--muted); margin-top:2px; font-size:11px; word-break:break-all; }
  .note { color:var(--muted); font-size:12px; padding:8px 16px 16px; }
</style>
<script src="https://cdn.jsdelivr.net/npm/hls.js@1.5.17/dist/hls.min.js"></script>
</head>
<body>
<header>
  <a href="__BACK__">← 返回</a>
  <h1>__TITLE__</h1>
</header>
<main>
  <div class="stage"><video id="v" controls playsinline></video></div>
  <aside class="sources">
    <h2>片源</h2>
    <div id="list"></div>
  </aside>
</main>
<p class="note">媒体库条目与外站在新标签打开；仅 HLS(m3u8) 在本页播放。不经本服务代传字节。</p>
<script>
const SOURCES = __SOURCES_JSON__;
const video = document.getElementById('v');
const list = document.getElementById('list');
let hls = null;

function stop() {
  if (hls) { hls.destroy(); hls = null; }
  video.removeAttribute('src');
  video.load();
}

function playInApp(url) {
  stop();
  if (window.Hls && Hls.isSupported()) {
    hls = new Hls({ enableWorker: true });
    hls.loadSource(url);
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, () => { video.play().catch(() => {}); });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = url;
    video.play().catch(() => {});
  } else {
    window.open(url, '_blank', 'noopener,noreferrer');
  }
}

function activate(btn) {
  [...list.querySelectorAll('.src')].forEach(el => el.classList.remove('active'));
  btn.classList.add('active');
}

SOURCES.forEach((src, i) => {
  const btn = document.createElement('button');
  btn.type = 'button';
  btn.className = 'src';
  btn.innerHTML = '<strong>' + src.label + '</strong><small>' + src.kind +
    (src.playable_in_app ? ' · 应用内' : ' · 外开') + '</small>';
  btn.onclick = () => {
    activate(btn);
    if (src.playable_in_app) playInApp(src.url);
    else window.open(src.url, '_blank', 'noopener,noreferrer');
  };
  list.appendChild(btn);
  if (i === 0) {
    activate(btn);
    if (src.playable_in_app) playInApp(src.url);
  }
});
</script>
</body>
</html>
"""


def render_player_page(
    *,
    title: str,
    back_href: str,
    sources: list[PlaySource],
) -> str:
    payload = [
        {
            "kind": s.kind,
            "label": s.label,
            "url": s.url,
            "playable_in_app": s.playable_in_app,
            "app_scheme": s.app_scheme,
            "probe_ok": s.probe_ok,
            "probe_reason": s.probe_reason,
        }
        for s in sources
    ]
    sources_json = json.dumps(payload, ensure_ascii=False).replace("</", "<\\/")
    safe_title = title.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return (
        PLAYER_HTML.replace("__TITLE__", safe_title)
        .replace("__BACK__", back_href)
        .replace("__SOURCES_JSON__", sources_json)
    )
