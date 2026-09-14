"""JavRanking static search-index client with on-disk cache (soft revalidate)."""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections.abc import Mapping
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..normalize_code import normalize_code, to_comparison_key

logger = logging.getLogger(__name__)

DEFAULT_BASE_URL = "https://javranking.cc"
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
    ) -> None:
        self.cache_dir = cache_dir
        self.client = client
        self.base_url = base_url.rstrip("/")
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

    @staticmethod
    def _has_ranking_structure(index: SearchIndex) -> bool:
        if not index.videos:
            return True
        return any(
            video.rank is not None or video.ranking_appearances for video in index.videos
        )

    async def fetch_and_cache(self) -> SearchIndex:
        client = self._require_client()
        manifest_url = f"{self.base_url}/{self.locale}/{_MANIFEST_NAME}"
        index_url = f"{self.base_url}/{self.locale}/{_INDEX_NAME}"

        manifest: SearchIndexManifest | None = None
        try:
            manifest_res = await client.get(manifest_url, headers={"User-Agent": BROWSER_UA})
            if manifest_res.status_code == 200 and "json" in (manifest_res.headers.get("content-type") or ""):
                manifest = SearchIndexManifest.model_validate(manifest_res.json())
                if manifest.schema_version != SUPPORTED_SCHEMA_VERSION:
                    manifest = None
        except Exception as exc:
            logger.warning("javranking manifest fetch failed: %s", exc)

        index_res = await client.get(index_url, headers={"User-Agent": BROWSER_UA})
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

    async def _revalidate(self, current: IndexCacheMeta) -> None:
        client = self._require_client()
        manifest_url = f"{self.base_url}/{self.locale}/{_MANIFEST_NAME}"
        res = await client.get(manifest_url, headers={"User-Agent": BROWSER_UA})
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

    def _require_client(self) -> httpx.AsyncClient:
        if self.client is None:
            raise RuntimeError("httpx AsyncClient is required to fetch javranking index")
        return self.client
