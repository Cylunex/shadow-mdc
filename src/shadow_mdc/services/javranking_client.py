"""JavRanking static search-index client with on-disk cache (soft revalidate)."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import time
import unicodedata
from collections.abc import Mapping, Sequence
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..normalize_code import normalize_code, to_comparison_key

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://javranking.cc"
FALLBACK_BASE_URL = "https://javranking.top"
CANDIDATE_BASE_URLS: tuple[str, ...] = (DEFAULT_BASE_URL, FALLBACK_BASE_URL)
DEFAULT_LOCALE = "zh-hans"
SUPPORTED_SCHEMA_VERSION = 2
SOFT_REVALIDATE_WINDOW_S = 12 * 60 * 60
HARD_TTL_S = 30 * 24 * 60 * 60

BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

_MANIFEST_NAME = "search-index-manifest.json"
_INDEX_NAME = "search-index.json"
_META_NAME = "meta.json"
_HONORS_NAME = "honors-by-code.json"
_ACTOR_HONORS_NAME = "actor-honors.json"
_LISTS_META_NAME = "lists-meta.json"

CURATED_LIST_SLUGS: tuple[str, ...] = (
    "most-awarded-videos",
    "most-awarded-actors",
    "most-awarded-male-actors",
)

CURATED_LIST_TITLES: dict[str, str] = {
    "most-awarded-videos": "神作 TOP100",
    "most-awarded-actors": "女优战力",
    "most-awarded-male-actors": "男优战力",
}

_CONNECT_ERRORS = (
    httpx.ConnectError,
    httpx.ConnectTimeout,
    httpx.TimeoutException,
    OSError,
)

_VIDEO_MD_RE = re.compile(
    r"^-\s*\[(?P<code>[A-Za-z0-9][A-Za-z0-9\-]*)\s*:\s*(?P<title>.+?)\]\((?P<url>https?://[^\s)]+)\)\s*$"
)
_ACTOR_HEAD_RE = re.compile(
    r"^##\s+(?P<name>.+?)\s*(?:\(⭐\s*(?P<score>[\d,]+)\))?\s*$"
)
_JSON_LD_RE = re.compile(
    r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(?P<body>.*?)</script>',
    re.IGNORECASE | re.DOTALL,
)
_VIDEO_ID_RE = re.compile(r"/videos/(?P<id>\d+)/?")
_ACTOR_SLUG_RE = re.compile(r"/actors/(?P<slug>[^/?#]+)/?")


class EntityLink(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    slug: str
    name: str
    gender: str | None = None


class RankingAppearance(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    slug: str
    name: str
    source: str | None = None
    scope: str | None = None
    year: int | None = None
    position: int = Field(ge=1)


class SearchVideo(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    video_id: int = Field(alias="videoId")
    code: str | None = None
    title: str
    score: float = 0.0
    rank: int | None = None
    cover_url: str | None = Field(default=None, alias="coverUrl")
    release_date: str | None = Field(default=None, alias="releaseDate")
    actor_links: tuple[EntityLink, ...] = Field(default=(), alias="actorLinks")
    ranking_appearances: tuple[RankingAppearance, ...] = Field(
        default=(), alias="rankingAppearances"
    )
    has_preview_video: bool = Field(default=False, alias="hasPreviewVideo")

    @field_validator("actor_links", "ranking_appearances", mode="before")
    @classmethod
    def _none_to_empty(cls, value: object) -> object:
        return () if value is None else value


class SearchIndex(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(alias="schemaVersion")
    videos: tuple[SearchVideo, ...] = ()


class SearchIndexManifest(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)

    schema_version: int = Field(alias="schemaVersion")
    locale: str
    revision: str
    generated_at: str = Field(alias="generatedAt")
    video_count: int = Field(alias="videoCount")
    byte_length: int = Field(alias="byteLength")


class IndexCacheMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = SUPPORTED_SCHEMA_VERSION
    locale: str
    revision: str
    cached_at: float
    last_checked_at: float


class WorkRankingHonor(BaseModel):
    """Ranking badge payload exposed on work detail."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    name: str
    source: str | None = None
    scope: str | None = None
    year: int | None = None
    position: int
    label: str
    url: str | None = None


class WorkJavRankingInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    video_id: int
    code: str | None = None
    rank: int | None = None
    score: float | None = None
    detail_url: str
    honors: tuple[WorkRankingHonor, ...] = ()
    compact_badge: str | None = None


class CuratedVideoEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1)
    code: str | None = None
    title: str
    video_id: int | None = None
    url: str | None = None
    cover_url: str | None = None


class CuratedActorAppearance(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str | None = None
    title: str
    video_id: int | None = None
    url: str | None = None


class CuratedActorEntry(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    position: int = Field(ge=1)
    name: str
    score: float | None = None
    slug: str | None = None
    url: str | None = None
    appearances: tuple[CuratedActorAppearance, ...] = ()


class CuratedList(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    slug: str
    title: str
    kind: str  # videos | actors
    locale: str
    base_url: str
    revision: str
    fetched_at: float
    source_format: str  # markdown | html-jsonld
    canonical_url: str | None = None
    videos: tuple[CuratedVideoEntry, ...] = ()
    actors: tuple[CuratedActorEntry, ...] = ()


class ActorJavRankingHonor(BaseModel):
    """Actor power-list badge for actor detail / API."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    list_slug: str
    list_title: str
    position: int
    score: float | None = None
    appearances: int = 0
    label: str
    url: str | None = None


class ActorJavRankingInfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    slug: str | None = None
    honors: tuple[ActorJavRankingHonor, ...] = ()
    compact_badge: str | None = None


class ListsCacheMeta(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    locale: str
    base_url: str
    fetched_at: float
    revisions: dict[str, str] = Field(default_factory=dict)


def sha256_hex16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def video_detail_url(video_id: int, *, locale: str = DEFAULT_LOCALE, base_url: str = DEFAULT_BASE_URL) -> str:
    root = base_url.rstrip("/")
    return f"{root}/{locale}/videos/{video_id}/"


def format_appearance_label(appearance: RankingAppearance) -> str:
    return f"#{appearance.position} {appearance.name}"


def format_overall_badge(rank: int) -> str:
    return f"#{rank} JavRanking"


def honors_from_video(
    video: SearchVideo,
    *,
    locale: str = DEFAULT_LOCALE,
    base_url: str = DEFAULT_BASE_URL,
) -> WorkJavRankingInfo:
    detail = video_detail_url(video.video_id, locale=locale, base_url=base_url)
    honors = tuple(
        WorkRankingHonor(
            slug=item.slug,
            name=item.name,
            source=item.source,
            scope=item.scope,
            year=item.year,
            position=item.position,
            label=format_appearance_label(item),
            url=detail,
        )
        for item in video.ranking_appearances
    )
    compact = format_overall_badge(video.rank) if video.rank is not None else None
    return WorkJavRankingInfo(
        video_id=video.video_id,
        code=video.code,
        rank=video.rank,
        score=float(video.score) if video.score is not None else None,
        detail_url=detail,
        honors=honors,
        compact_badge=compact,
    )


def build_honors_map(
    videos: tuple[SearchVideo, ...] | list[SearchVideo],
    *,
    locale: str = DEFAULT_LOCALE,
    base_url: str = DEFAULT_BASE_URL,
) -> dict[str, WorkJavRankingInfo]:
    """Map comparison keys → ranking info. Ambiguous keys are skipped (fail closed)."""

    buckets: dict[str, list[SearchVideo]] = {}
    for video in videos:
        if not video.code:
            continue
        key = to_comparison_key(video.code)
        if not key:
            continue
        buckets.setdefault(key, []).append(video)

    result: dict[str, WorkJavRankingInfo] = {}
    for key, matches in buckets.items():
        if len(matches) != 1:
            logger.warning("javranking ambiguous comparison key %s (%d videos)", key, len(matches))
            continue
        result[key] = honors_from_video(matches[0], locale=locale, base_url=base_url)
    return result



def normalize_actor_key(name: str) -> str:
    return unicodedata.normalize("NFKC", name).casefold().strip()


def curated_list_kind(slug: str) -> str:
    if slug == "most-awarded-videos":
        return "videos"
    return "actors"


def curated_list_path(slug: str, *, locale: str = DEFAULT_LOCALE, markdown: bool = True) -> str:
    cleaned = slug.strip().strip("/")
    if markdown:
        return f"/{locale}/{cleaned}.md"
    return f"/{locale}/{cleaned}/"


def extract_video_id(url: str | None) -> int | None:
    if not url:
        return None
    match = _VIDEO_ID_RE.search(url)
    if not match:
        return None
    return int(match.group("id"))


def extract_actor_slug(url: str | None) -> str | None:
    if not url:
        return None
    match = _ACTOR_SLUG_RE.search(url)
    return match.group("slug") if match else None


def _parse_score_token(raw: str | None) -> float | None:
    if not raw:
        return None
    try:
        return float(raw.replace(",", ""))
    except ValueError:
        return None


def parse_code_title_label(label: str) -> tuple[str | None, str]:
    text = label.strip()
    if ":" in text:
        code_part, title_part = text.split(":", 1)
        code = normalize_code(code_part.strip()) or code_part.strip() or None
        return code, title_part.strip() or text
    # JSON-LD names often look like "IPX-811 title..."
    parts = text.split(None, 1)
    if parts and re.fullmatch(r"[A-Za-z0-9]+(?:-[A-Za-z0-9]+)+", parts[0]):
        code = normalize_code(parts[0]) or parts[0]
        return code, (parts[1] if len(parts) > 1 else parts[0])
    return None, text


def parse_curated_videos_markdown(text: str) -> tuple[str | None, list[CuratedVideoEntry]]:
    canonical: str | None = None
    entries: list[CuratedVideoEntry] = []
    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("canonical html:"):
            canonical = stripped.split(":", 1)[1].strip() or None
            continue
        match = _VIDEO_MD_RE.match(stripped)
        if not match:
            continue
        code = normalize_code(match.group("code")) or match.group("code")
        url = match.group("url")
        entries.append(
            CuratedVideoEntry(
                position=len(entries) + 1,
                code=code,
                title=match.group("title").strip(),
                video_id=extract_video_id(url),
                url=url,
            )
        )
    return canonical, entries


def parse_curated_actors_markdown(text: str) -> tuple[str | None, list[CuratedActorEntry]]:
    canonical: str | None = None
    entries: list[CuratedActorEntry] = []
    current_name: str | None = None
    current_score: float | None = None
    current_apps: list[CuratedActorAppearance] = []

    def flush() -> None:
        nonlocal current_name, current_score, current_apps
        if not current_name:
            return
        entries.append(
            CuratedActorEntry(
                position=len(entries) + 1,
                name=current_name,
                score=current_score,
                appearances=tuple(current_apps),
            )
        )
        current_name = None
        current_score = None
        current_apps = []

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.lower().startswith("canonical html:"):
            canonical = stripped.split(":", 1)[1].strip() or None
            continue
        if stripped.startswith("# ") and not stripped.startswith("## "):
            continue
        head = _ACTOR_HEAD_RE.match(stripped)
        skip_rules = stripped.startswith("## 计算") or "计算规则" in stripped or "計算" in stripped
        if head and not skip_rules:
            flush()
            current_name = head.group("name").strip()
            current_score = _parse_score_token(head.group("score"))
            current_apps = []
            continue
        match = _VIDEO_MD_RE.match(stripped)
        if match and current_name is not None:
            code = normalize_code(match.group("code")) or match.group("code")
            url = match.group("url")
            current_apps.append(
                CuratedActorAppearance(
                    code=code,
                    title=match.group("title").strip(),
                    video_id=extract_video_id(url),
                    url=url,
                )
            )
    flush()
    return canonical, entries


def parse_item_list_json_ld(
    html: str, *, kind: str
) -> tuple[str | None, list[CuratedVideoEntry], list[CuratedActorEntry]]:
    canonical: str | None = None
    videos: list[CuratedVideoEntry] = []
    actors: list[CuratedActorEntry] = []
    for match in _JSON_LD_RE.finditer(html):
        body = match.group("body").strip()
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            continue
        candidates = payload if isinstance(payload, list) else [payload]
        for item in candidates:
            if not isinstance(item, dict):
                continue
            if item.get("@type") != "ItemList":
                continue
            url = item.get("url")
            if isinstance(url, str) and url:
                canonical = url
            elements = item.get("itemListElement")
            if not isinstance(elements, list):
                continue
            for element in elements:
                if not isinstance(element, dict):
                    continue
                position_raw = element.get("position")
                try:
                    position = int(position_raw)
                except (TypeError, ValueError):
                    position = len(videos if kind == "videos" else actors) + 1
                name = str(element.get("name") or "").strip()
                item_url = element.get("url")
                item_url_s = item_url if isinstance(item_url, str) else None
                if kind == "videos":
                    code, title = parse_code_title_label(name)
                    videos.append(
                        CuratedVideoEntry(
                            position=position,
                            code=code,
                            title=title,
                            video_id=extract_video_id(item_url_s),
                            url=item_url_s,
                        )
                    )
                else:
                    actors.append(
                        CuratedActorEntry(
                            position=position,
                            name=name,
                            slug=extract_actor_slug(item_url_s),
                            url=item_url_s,
                        )
                    )
            if videos or actors:
                return canonical, videos, actors
    return canonical, videos, actors


def build_actor_honors_map(
    lists: Sequence[CuratedList],
) -> dict[str, ActorJavRankingInfo]:
    buckets: dict[str, list[ActorJavRankingHonor]] = {}
    names: dict[str, str] = {}
    slugs: dict[str, str | None] = {}
    for curated in lists:
        if curated.kind != "actors":
            continue
        list_title = CURATED_LIST_TITLES.get(curated.slug, curated.title)
        for actor in curated.actors:
            key = normalize_actor_key(actor.name)
            if not key:
                continue
            names.setdefault(key, actor.name)
            if actor.slug:
                slugs[key] = actor.slug
            elif key not in slugs:
                slugs[key] = None
            label = f"#{actor.position} {list_title}"
            if actor.score is not None:
                label = f"{label} · {actor.score:g}"
            buckets.setdefault(key, []).append(
                ActorJavRankingHonor(
                    list_slug=curated.slug,
                    list_title=list_title,
                    position=actor.position,
                    score=actor.score,
                    appearances=len(actor.appearances),
                    label=label,
                    url=actor.url or curated.canonical_url,
                )
            )
    result: dict[str, ActorJavRankingInfo] = {}
    for key, honors in buckets.items():
        ordered = tuple(sorted(honors, key=lambda item: (item.list_slug, item.position)))
        best = min(ordered, key=lambda item: item.position)
        result[key] = ActorJavRankingInfo(
            name=names[key],
            slug=slugs.get(key),
            honors=ordered,
            compact_badge=best.label,
        )
    return result


def top250_year_slugs(videos: Sequence[SearchVideo]) -> list[tuple[str, int | None, str]]:
    """Return unique (slug, year, name) for yearly TOP250-style rankings."""

    seen: dict[str, tuple[str, int | None, str]] = {}
    for video in videos:
        for appearance in video.ranking_appearances:
            slug = appearance.slug.strip()
            if not slug:
                continue
            lowered = slug.casefold()
            if "top250" not in lowered and "top-250" not in lowered:
                continue
            if slug not in seen:
                seen[slug] = (slug, appearance.year, appearance.name)
    return sorted(seen.values(), key=lambda item: (-(item[1] or 0), item[0]))


def parse_search_index(payload: Mapping[str, object] | str | bytes) -> SearchIndex:
    data = json.loads(payload) if isinstance(payload, (str, bytes)) else dict(payload)
    if not data.get("schemaVersion"):
        data["schemaVersion"] = SUPPORTED_SCHEMA_VERSION
    videos = data.get("videos")
    if isinstance(videos, list):
        for item in videos:
            if isinstance(item, dict):
                item.setdefault("rankingAppearances", [])
                if not isinstance(item.get("hasPreviewVideo"), bool):
                    item["hasPreviewVideo"] = False
    index = SearchIndex.model_validate(data)
    if index.schema_version != SUPPORTED_SCHEMA_VERSION:
        raise ValueError(f"unsupported schemaVersion: {index.schema_version}")
    return index


class JavRankingIndexCache:
    """Filesystem cache under ``data/javranking/`` with soft revalidation."""

    def __init__(
        self,
        cache_dir: Path,
        *,
        client: httpx.AsyncClient | None = None,
        base_url: str = DEFAULT_BASE_URL,
        locale: str = DEFAULT_LOCALE,
        soft_revalidate_s: float = SOFT_REVALIDATE_WINDOW_S,
        hard_ttl_s: float = HARD_TTL_S,
        candidate_base_urls: Sequence[str] | None = None,
    ) -> None:
        self.cache_dir = cache_dir
        self.client = client
        preferred = base_url.rstrip("/")
        ordered: list[str] = []
        for item in (preferred, *(candidate_base_urls or CANDIDATE_BASE_URLS)):
            root = item.rstrip("/")
            if root and root not in ordered:
                ordered.append(root)
        self._candidate_base_urls = tuple(ordered)
        self.base_url = preferred
        self.locale = locale
        self.soft_revalidate_s = soft_revalidate_s
        self.hard_ttl_s = hard_ttl_s

    @property
    def meta_path(self) -> Path:
        return self.cache_dir / _META_NAME

    @property
    def index_path(self) -> Path:
        return self.cache_dir / _INDEX_NAME

    @property
    def honors_path(self) -> Path:
        return self.cache_dir / _HONORS_NAME

    @property
    def actor_honors_path(self) -> Path:
        return self.cache_dir / _ACTOR_HONORS_NAME

    @property
    def lists_meta_path(self) -> Path:
        return self.cache_dir / _LISTS_META_NAME

    def list_cache_path(self, slug: str) -> Path:
        return self.cache_dir / f"list-{slug}.json"

    def read_meta(self) -> IndexCacheMeta | None:
        if not self.meta_path.is_file():
            return None
        try:
            return IndexCacheMeta.model_validate_json(self.meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def read_cached_index(self) -> SearchIndex | None:
        if not self.index_path.is_file():
            return None
        try:
            return parse_search_index(self.index_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def read_honors_map(self) -> dict[str, WorkJavRankingInfo]:
        if not self.honors_path.is_file():
            return {}
        try:
            payload = json.loads(self.honors_path.read_text(encoding="utf-8"))
            raw = payload.get("by_key") if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                return {}
            return {
                str(key): WorkJavRankingInfo.model_validate(value)
                for key, value in raw.items()
                if isinstance(value, dict)
            }
        except Exception:
            return {}

    def lookup_honors(self, code: str | None) -> WorkJavRankingInfo | None:
        if not code:
            return None
        key = to_comparison_key(normalize_code(code) or code)
        if not key:
            return None
        return self.read_honors_map().get(key)

    def read_actor_honors_map(self) -> dict[str, ActorJavRankingInfo]:
        if not self.actor_honors_path.is_file():
            return {}
        try:
            payload = json.loads(self.actor_honors_path.read_text(encoding="utf-8"))
            raw = payload.get("by_key") if isinstance(payload, dict) else None
            if not isinstance(raw, dict):
                return {}
            return {
                str(key): ActorJavRankingInfo.model_validate(value)
                for key, value in raw.items()
                if isinstance(value, dict)
            }
        except Exception:
            return {}

    def lookup_actor_honors(
        self, name: str | None, *, aliases: Sequence[str] = ()
    ) -> ActorJavRankingInfo | None:
        if not name and not aliases:
            return None
        honors = self.read_actor_honors_map()
        for candidate in (name, *aliases):
            if not candidate:
                continue
            hit = honors.get(normalize_actor_key(candidate))
            if hit is not None:
                return hit
        return None

    def read_lists_meta(self) -> ListsCacheMeta | None:
        if not self.lists_meta_path.is_file():
            return None
        try:
            return ListsCacheMeta.model_validate_json(self.lists_meta_path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def read_curated_list(self, slug: str) -> CuratedList | None:
        path = self.list_cache_path(slug)
        if not path.is_file():
            return None
        try:
            return CuratedList.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            return None

    def write_cache(
        self,
        *,
        index: SearchIndex,
        raw_text: str,
        revision: str,
        cached_at: float | None = None,
        last_checked_at: float | None = None,
    ) -> IndexCacheMeta:
        now = time.time()
        meta = IndexCacheMeta(
            schema_version=SUPPORTED_SCHEMA_VERSION,
            locale=self.locale,
            revision=revision,
            cached_at=cached_at if cached_at is not None else now,
            last_checked_at=last_checked_at if last_checked_at is not None else now,
        )
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.index_path.write_text(raw_text, encoding="utf-8")
        self.meta_path.write_text(meta.model_dump_json(indent=2), encoding="utf-8")
        honors = build_honors_map(index.videos, locale=self.locale, base_url=self.base_url)
        honors_payload = {
            "revision": revision,
            "locale": self.locale,
            "by_key": {key: value.model_dump(mode="json") for key, value in honors.items()},
        }
        self.honors_path.write_text(
            json.dumps(honors_payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return meta

    def write_curated_list(self, curated: CuratedList) -> CuratedList:
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.list_cache_path(curated.slug).write_text(
            curated.model_dump_json(indent=2),
            encoding="utf-8",
        )
        meta = self.read_lists_meta()
        revisions = dict(meta.revisions) if meta is not None else {}
        revisions[curated.slug] = curated.revision
        lists_meta = ListsCacheMeta(
            locale=self.locale,
            base_url=self.base_url,
            fetched_at=curated.fetched_at,
            revisions=revisions,
        )
        self.lists_meta_path.write_text(lists_meta.model_dump_json(indent=2), encoding="utf-8")
        if curated.kind == "actors" or curated.slug in {
            "most-awarded-actors",
            "most-awarded-male-actors",
        }:
            self._refresh_actor_honors_cache()
        return curated

    def _refresh_actor_honors_cache(self) -> dict[str, ActorJavRankingInfo]:
        lists: list[CuratedList] = []
        for slug in ("most-awarded-actors", "most-awarded-male-actors"):
            curated = self.read_curated_list(slug)
            if curated is not None:
                lists.append(curated)
        honors = build_actor_honors_map(lists)
        payload = {
            "locale": self.locale,
            "base_url": self.base_url,
            "fetched_at": time.time(),
            "by_key": {key: value.model_dump(mode="json") for key, value in honors.items()},
        }
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.actor_honors_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return honors

    async def load_index(self, *, force_refresh: bool = False) -> SearchIndex:
        now = time.time()
        meta = self.read_meta()
        if (
            not force_refresh
            and meta is not None
            and meta.locale == self.locale
            and meta.schema_version == SUPPORTED_SCHEMA_VERSION
            and (now - meta.cached_at) <= self.hard_ttl_s
        ):
            cached = self.read_cached_index()
            if cached is not None and self._has_ranking_structure(cached):
                if (now - meta.last_checked_at) >= self.soft_revalidate_s:
                    try:
                        await self._revalidate(meta)
                    except Exception as exc:
                        logger.warning("javranking soft revalidate failed: %s", exc)
                return cached
        return await self.fetch_and_cache()

    async def load_curated_list(self, slug: str, *, force_refresh: bool = False) -> CuratedList:
        if slug not in CURATED_LIST_SLUGS:
            raise ValueError(f"unsupported curated list slug: {slug}")
        now = time.time()
        cached = self.read_curated_list(slug)
        if (
            not force_refresh
            and cached is not None
            and cached.locale == self.locale
            and (now - cached.fetched_at) <= self.hard_ttl_s
        ):
            return cached
        return await self.fetch_curated_list(slug)

    async def load_curated_lists(
        self,
        *,
        force_refresh: bool = False,
        slugs: Sequence[str] | None = None,
    ) -> dict[str, CuratedList]:
        targets = tuple(slugs) if slugs is not None else CURATED_LIST_SLUGS
        result: dict[str, CuratedList] = {}
        for slug in targets:
            result[slug] = await self.load_curated_list(slug, force_refresh=force_refresh)
        return result

    @staticmethod
    def _has_ranking_structure(index: SearchIndex) -> bool:
        if not index.videos:
            return True
        return any(
            video.rank is not None or video.ranking_appearances for video in index.videos
        )

    async def fetch_and_cache(self) -> SearchIndex:
        manifest: SearchIndexManifest | None = None
        try:
            manifest_res = await self._get_with_fallback(f"/{self.locale}/{_MANIFEST_NAME}")
            if manifest_res.status_code == 200 and "json" in (
                manifest_res.headers.get("content-type") or ""
            ):
                manifest = SearchIndexManifest.model_validate(manifest_res.json())
                if manifest.schema_version != SUPPORTED_SCHEMA_VERSION:
                    manifest = None
        except Exception as exc:
            logger.warning("javranking manifest fetch failed: %s", exc)

        index_res = await self._get_with_fallback(f"/{self.locale}/{_INDEX_NAME}")
        index_res.raise_for_status()
        raw_text = index_res.text
        index = parse_search_index(raw_text)
        revision = (
            manifest.revision
            if manifest is not None and manifest.revision
            else sha256_hex16(raw_text)
        )
        self.write_cache(index=index, raw_text=raw_text, revision=revision)
        return index

    async def fetch_curated_list(self, slug: str) -> CuratedList:
        kind = curated_list_kind(slug)
        title = CURATED_LIST_TITLES.get(slug, slug)
        md_path = curated_list_path(slug, locale=self.locale, markdown=True)
        html_path = curated_list_path(slug, locale=self.locale, markdown=False)
        fetched_at = time.time()
        source_format = "markdown"
        canonical: str | None = None
        videos: list[CuratedVideoEntry] = []
        actors: list[CuratedActorEntry] = []
        raw_for_revision = ""

        try:
            md_res = await self._get_with_fallback(md_path)
            ctype = (md_res.headers.get("content-type") or "").casefold()
            if md_res.status_code == 200 and ("markdown" in ctype or md_path.endswith(".md")):
                raw_for_revision = md_res.text
                if kind == "videos":
                    canonical, videos = parse_curated_videos_markdown(md_res.text)
                else:
                    canonical, actors = parse_curated_actors_markdown(md_res.text)
                if videos or actors:
                    source_format = "markdown"
                else:
                    raw_for_revision = ""
        except Exception as exc:
            logger.warning("javranking markdown list fetch failed (%s): %s", slug, exc)

        if not videos and not actors:
            html_res = await self._get_with_fallback(html_path)
            html_res.raise_for_status()
            raw_for_revision = html_res.text
            canonical, videos, actors = parse_item_list_json_ld(html_res.text, kind=kind)
            source_format = "html-jsonld"
            if not videos and not actors:
                raise ValueError(f"javranking curated list empty: {slug}")

        curated = CuratedList(
            slug=slug,
            title=title,
            kind=kind,
            locale=self.locale,
            base_url=self.base_url,
            revision=sha256_hex16(raw_for_revision),
            fetched_at=fetched_at,
            source_format=source_format,
            canonical_url=canonical or f"{self.base_url}{html_path}",
            videos=tuple(videos),
            actors=tuple(actors),
        )
        return self.write_curated_list(curated)

    async def _revalidate(self, current: IndexCacheMeta) -> None:
        res = await self._get_with_fallback(f"/{self.locale}/{_MANIFEST_NAME}")
        if res.status_code != 200:
            return
        if "json" not in (res.headers.get("content-type") or ""):
            return
        manifest = SearchIndexManifest.model_validate(res.json())
        if manifest.schema_version != SUPPORTED_SCHEMA_VERSION or not manifest.revision:
            return
        now = time.time()
        if manifest.revision == current.revision:
            updated = current.model_copy(update={"last_checked_at": now})
            self.meta_path.write_text(updated.model_dump_json(indent=2), encoding="utf-8")
            return
        await self.fetch_and_cache()

    async def _get_with_fallback(self, path: str) -> httpx.Response:
        """GET ``path`` trying preferred then fallback hosts on DNS/connect failure."""

        client = self._require_client()
        relative = path if path.startswith("/") else f"/{path}"
        errors: list[str] = []
        for root in self._candidate_base_urls:
            url = f"{root}{relative}"
            try:
                response = await client.get(url, headers={"User-Agent": BROWSER_UA})
            except _CONNECT_ERRORS as exc:
                errors.append(f"{root}: {type(exc).__name__}: {exc}")
                logger.warning("javranking connect failed for %s: %s", url, exc)
                continue
            self.base_url = root
            return response
        detail = "; ".join(errors) if errors else "no candidate hosts"
        raise httpx.ConnectError(f"javranking unreachable ({detail})")

    def _require_client(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("httpx AsyncClient is required to fetch javranking index")
        return self.client
