"""JavDB App JSON API client (rankings, TOP250, movie detail, magnets).

Ported from FlanChanXwO/javdb-cli (MIT): the ``jdsignature`` header
(``internal/javdb/protocol/signature``), the public device params and envelope
handling (``internal/javdb/appapi/client``) and the rankings / TOP250 / magnets
endpoint shapes. The app API is a stable JSON contract, so it is the primary
source for JavDB rankings; ``/rankings/movies`` HTML scraping stays as fallback
because the site layout and URLs changed twice in 2026-09.

Read-only use only: no login flow is implemented. ``/api/v1/movies/top`` (TOP250)
needs a bearer token; pass one via ``SHADOW_MDC_JAVDB_API_TOKEN`` to enable it.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import httpx

from ..config import Settings
from ..domain import Artwork, ProviderRecord
from ..enums import ContentFamily
from ..identity import extract_code
from ..media.magnets import MagnetLink, magnet_quality_flags

# Precomputed in the app from access key "30820" + CONST_PREFIX/CONST_SUFFIX
# (JavDB.apk 1.9.28). Only the timestamp changes per request.
SIGNATURE_PREFIX = (
    "71cf27bb3c0bcdf207b64abecddc970098c7421ee7203b9cdae54478478a199e"
    "7d5a6e1a57691123c1a931c057842fb73ba3b3c83bcd69c17ccf174081e3d8aa"
)
SIGNATURE_SUFFIX = "lpw6vgqzsp"
APP_VERSION = "1.9.28"
APP_VERSION_NUMBER = "10928"
APP_USER_AGENT = "Dart/3.4 (dart:io)"
DEFAULT_API_HOSTS: tuple[str, ...] = ("https://jdforrepam.com", "https://javdb.com")

RankingZone = Literal["censored", "uncensored", "western", "fc2"]
RankingPeriod = Literal["daily", "weekly", "monthly"]
ZONES: dict[str, int] = {"censored": 0, "uncensored": 1, "western": 2, "fc2": 3}
PERIODS: tuple[str, ...] = ("daily", "weekly", "monthly")
_PERIOD_ALIASES = {"day": "daily", "week": "weekly", "month": "monthly"}
# The app answers ``success: 0`` with these actions when a bearer token is needed.
_AUTH_ACTIONS = frozenset(
    {"JWTVerificationError", "Unauthorized", "LoginRequired", "TokenInvalid", "TokenExpired"}
)
# JavDB gender codes on actor entities (0 = female, 1 = male).
_FEMALE = 0
# Category words that must never be stored as performers (see ca2675e nav bug).
_NON_PERFORMER_LABELS = frozenset(
    {"有碼", "有码", "無碼", "无码", "歐美", "欧美", "FC2", "動漫", "动漫", "素人", "censored", "uncensored", "western"}
)


def sign(timestamp: int | None = None) -> str:
    """Return a ``jdsignature`` header value (``{ts}.{suffix}.{md5(ts+prefix)}``)."""

    ts = int(timestamp if timestamp and timestamp > 0 else time.time())
    digest = hashlib.md5(f"{ts}{SIGNATURE_PREFIX}".encode()).hexdigest()
    return f"{ts}.{SIGNATURE_SUFFIX}.{digest}"


def normalize_period(period: str) -> str:
    value = period.strip().casefold()
    value = _PERIOD_ALIASES.get(value, value)
    if value not in PERIODS:
        raise ValueError(f"period must be one of {PERIODS}")
    return value


def zone_code(zone: str) -> int:
    try:
        return ZONES[zone.strip().casefold()]
    except KeyError as exc:
        raise ValueError(f"zone must be one of {tuple(ZONES)}") from exc


class JavDBApiError(RuntimeError):
    """The app API answered ``success: 0`` or a non-JSON / HTTP error body."""

    def __init__(self, message: str, *, action: str | None = None, status: int | None = None) -> None:
        super().__init__(message)
        self.action = action
        self.status = status


class JavDBApiAuthRequired(JavDBApiError):
    """Endpoint needs a bearer token (e.g. TOP250)."""


@dataclass(frozen=True)
class JavDBApiMovie:
    """One movie row from a ranking / list payload."""

    id: str
    number: str | None
    title: str
    cover_url: str | None
    thumb_url: str | None
    release_date: date | None
    duration_minutes: int | None
    score: float | None
    magnets_count: int | None
    has_cnsub: bool
    zone: str | None = None


def _as_str(value: object) -> str | None:
    if isinstance(value, str):
        cleaned = value.strip()
        return cleaned or None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return str(value)
    return None


def _as_int(value: object) -> int | None:
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.strip().isdigit():
        return int(value.strip())
    return None


def _as_float(value: object) -> float | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip())
        except ValueError:
            return None
    return None


def _as_date(value: object) -> date | None:
    text = _as_str(value)
    if not text:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        return None


def _truthy(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().casefold() in {"1", "true", "yes"}
    return False


def parse_movie_row(row: Mapping[str, Any], *, zone: str | None = None) -> JavDBApiMovie | None:
    movie_id = _as_str(row.get("id"))
    if not movie_id:
        return None
    number = _as_str(row.get("number"))
    title = _as_str(row.get("origin_title")) or _as_str(row.get("title")) or number or movie_id
    score = _as_float(row.get("score"))
    return JavDBApiMovie(
        id=movie_id,
        number=number,
        title=title,
        cover_url=_as_str(row.get("cover_url")),
        thumb_url=_as_str(row.get("thumb_url")),
        release_date=_as_date(row.get("release_date")),
        duration_minutes=_as_int(row.get("duration")),
        score=score if score is not None and 0 <= score <= 5 else None,
        magnets_count=_as_int(row.get("magnets_count")),
        has_cnsub=_truthy(row.get("has_cnsub")),
        zone=zone,
    )


def parse_movie_rows(data: Mapping[str, Any], *, zone: str | None = None) -> list[JavDBApiMovie]:
    rows = data.get("movies")
    if not isinstance(rows, list):
        return []
    movies: list[JavDBApiMovie] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        movie = parse_movie_row(row, zone=zone)
        if movie is None or movie.id in seen:
            continue
        seen.add(movie.id)
        movies.append(movie)
    return movies


def female_actor_names(actors: object) -> tuple[str, ...]:
    """Female performers only (gender 0); all names when no gender is reported.

    Category words (有碼/無碼/歐美/FC2…) are never performers.
    """

    if not isinstance(actors, list):
        return ()
    entries: list[tuple[str, int | None]] = []
    for actor in actors:
        if not isinstance(actor, Mapping):
            continue
        name = _as_str(actor.get("name"))
        if not name or name in _NON_PERFORMER_LABELS or name in {entry[0] for entry in entries}:
            continue
        entries.append((name, _as_int(actor.get("gender"))))
    if any(gender is not None for _name, gender in entries):
        return tuple(name for name, gender in entries if gender == _FEMALE)
    return tuple(name for name, _gender in entries)


def parse_movie_detail(data: Mapping[str, Any], *, site_base_url: str = "https://javdb.com") -> ProviderRecord:
    """Map ``/api/v4/movies/{id}`` ``data`` into a provider record (``provider=javdb``)."""

    movie = data.get("movie") if isinstance(data.get("movie"), Mapping) else data
    assert isinstance(movie, Mapping)
    movie_id = _as_str(movie.get("id"))
    if not movie_id:
        raise JavDBApiError("movie detail missing id")
    number = _as_str(movie.get("number"))
    parsed_code, family = extract_code(number) if number else (None, ContentFamily.UNKNOWN)
    title = _as_str(movie.get("origin_title")) or _as_str(movie.get("title")) or number
    if not title:
        raise JavDBApiError("movie detail missing title")
    duration = _as_int(movie.get("duration"))
    score = _as_float(movie.get("score"))
    rating = score if score is not None and 0 < score <= 5 else None
    tags_raw = movie.get("tags")
    tags: list[str] = []
    if isinstance(tags_raw, list):
        for tag in tags_raw:
            name = _as_str(tag.get("name")) if isinstance(tag, Mapping) else None
            if name and name not in tags:
                tags.append(name)
    artwork: list[Artwork] = []
    cover = _as_str(movie.get("cover_url")) or _as_str(movie.get("thumb_url"))
    if cover and cover.startswith(("http://", "https://")):
        artwork.append(Artwork.model_validate({"url": cover, "kind": "thumb"}))
    previews = movie.get("preview_images")
    if isinstance(previews, list):
        for preview in previews[:30]:
            if not isinstance(preview, Mapping):
                continue
            url = _as_str(preview.get("large_url")) or _as_str(preview.get("thumb_url"))
            if url and url.startswith(("http://", "https://")):
                artwork.append(Artwork.model_validate({"url": url, "kind": "sample"}))
    director = _as_str(movie.get("director_name"))
    return ProviderRecord(
        provider="javdb",
        external_id=movie_id,
        source_url=f"{site_base_url.rstrip('/')}/v/{movie_id}",
        code=parsed_code,
        title=title,
        original_title=title,
        family=family,
        release_date=_as_date(movie.get("release_date")),
        runtime_seconds=duration * 60 if duration and duration > 0 else None,
        studio=_as_str(movie.get("maker_name")),
        label=_as_str(movie.get("publisher_name")),
        series=_as_str(movie.get("series_name")),
        plot=_as_str(movie.get("summary")),
        actors=female_actor_names(movie.get("actors")),
        directors=(director,) if director else (),
        tags=tuple(tags),
        artwork=tuple(artwork),
        language="zh",
        rating=rating,
        rating_max=5.0 if rating is not None else None,
        rating_count=_as_int(movie.get("reviews_count")) if rating is not None else None,
    )


def parse_magnets(data: Mapping[str, Any]) -> tuple[MagnetLink, ...]:
    """Map ``/api/v1/movies/{id}/magnets`` into magnet links (``size`` is MiB)."""

    rows = data.get("magnets")
    if not isinstance(rows, list):
        return ()
    found: list[MagnetLink] = []
    seen: set[str] = set()
    for row in rows:
        if not isinstance(row, Mapping):
            continue
        digest = (_as_str(row.get("hash")) or "").upper()
        if len(digest) not in {32, 40} or digest in seen:
            continue
        seen.add(digest)
        name = _as_str(row.get("name"))
        size_mib = _as_int(row.get("size"))
        subtitle, hd = magnet_quality_flags(name)
        found.append(
            MagnetLink(
                provider="javdb",
                info_hash=digest,
                uri=f"magnet:?xt=urn:btih:{digest}",
                name=name,
                size_bytes=size_mib * 1024 * 1024 if size_mib and size_mib > 0 else None,
                has_subtitle=_truthy(row.get("cnsub")) or subtitle,
                hd=_truthy(row.get("hd")) or hd,
                files_count=_as_int(row.get("files_count")),
            )
        )
    return tuple(found)


class JavDBAppApi:
    """Signed, read-only client for the JavDB app API."""

    def __init__(
        self,
        client: httpx.AsyncClient,
        *,
        hosts: Sequence[str] = DEFAULT_API_HOSTS,
        site_base_url: str = "https://javdb.com",
        token: str | None = None,
        language: str = "zh",
        device_uuid: str | None = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        cleaned = tuple(host.rstrip("/") for host in hosts if host and host.strip())
        if not cleaned:
            raise ValueError("at least one JavDB API host is required")
        self._client = client
        self._hosts = cleaned
        self._site_base_url = site_base_url.rstrip("/")
        self._token = token or None
        self._language = language
        self._clock = clock
        self._public = {
            "app_channel": "official",
            "app_version": APP_VERSION,
            "app_version_number": APP_VERSION_NUMBER,
            "platform": "android",
            "system_version": "13",
            "device_model": "Pixel 6",
            "device_name": "Pixel",
            "device_uuid": device_uuid or str(uuid.uuid4()),
        }

    @property
    def has_token(self) -> bool:
        return self._token is not None

    @property
    def site_base_url(self) -> str:
        return self._site_base_url

    def _headers(self) -> dict[str, str]:
        headers = {
            "jdsignature": sign(int(self._clock())),
            "accept-language": self._language,
            "user-agent": APP_USER_AGENT,
            "accept": "application/json",
        }
        if self._token:
            headers["authorization"] = f"Bearer {self._token}"
        return headers

    async def get_json(self, path: str, params: Mapping[str, str] | None = None) -> dict[str, Any]:
        """Signed GET; returns the envelope ``data`` object. Tries each host on transport errors."""

        query = dict(self._public)
        for key, value in (params or {}).items():
            if value != "":
                query[key] = value
        last_error: Exception | None = None
        for host in self._hosts:
            try:
                response = await self._client.get(f"{host}{path}", params=query, headers=self._headers())
            except httpx.HTTPError as exc:
                last_error = JavDBApiError(f"{host}: {type(exc).__name__}: {exc}".strip())
                continue
            if response.status_code >= 500 or response.status_code in {403, 429}:
                last_error = JavDBApiError(f"{host}: HTTP {response.status_code}", status=response.status_code)
                continue
            if response.status_code >= 400:
                raise JavDBApiError(f"HTTP {response.status_code}", status=response.status_code)
            try:
                payload = response.json()
            except ValueError:
                last_error = JavDBApiError(f"{host}: non-JSON response")
                continue
            if not isinstance(payload, dict):
                last_error = JavDBApiError(f"{host}: unexpected JSON envelope")
                continue
            if not _truthy(payload.get("success")):
                action = _as_str(payload.get("action"))
                message = _as_str(payload.get("message")) or "request failed"
                if action in _AUTH_ACTIONS:
                    raise JavDBApiAuthRequired(f"{action}: {message}", action=action)
                raise JavDBApiError(f"{action or 'error'}: {message}", action=action)
            data = payload.get("data")
            return data if isinstance(data, dict) else {}
        raise last_error or JavDBApiError("no JavDB API host reachable")

    async def rankings(self, zone: str = "censored", period: str = "daily") -> list[JavDBApiMovie]:
        """``GET /api/v1/rankings`` — one fixed list (~60 rows) per zone × period."""

        normalized_zone = zone.strip().casefold()
        data = await self.get_json(
            "/api/v1/rankings",
            {"type": str(zone_code(normalized_zone)), "period": normalize_period(period)},
        )
        return parse_movie_rows(data, zone=normalized_zone)

    async def top250(
        self,
        *,
        zone: str | None = None,
        year: int | None = None,
        page: int = 1,
        limit: int = 50,
    ) -> list[JavDBApiMovie]:
        """``GET /api/v1/movies/top`` (needs a token). ``year`` wins over ``zone``."""

        if year is not None:
            type_, value = "year", str(year)
        elif zone:
            type_, value = "video_type", str(zone_code(zone))
        else:
            type_, value = "all", "all"
        data = await self.get_json(
            "/api/v1/movies/top",
            {
                "type": type_,
                "type_value": value,
                "start_rank": "1",
                "page": str(max(1, page)),
                "limit": str(max(1, min(limit, 100))),
                "ignore_watched": "false",
            },
        )
        return parse_movie_rows(data, zone=zone)

    async def movie_detail(self, movie_id: str) -> ProviderRecord:
        data = await self.get_json(f"/api/v4/movies/{movie_id.strip()}")
        return parse_movie_detail(data, site_base_url=self._site_base_url)

    async def magnets(self, movie_id: str) -> tuple[MagnetLink, ...]:
        data = await self.get_json(f"/api/v1/movies/{movie_id.strip()}/magnets")
        return parse_magnets(data)

    def movie_url(self, movie_id: str) -> str:
        return f"{self._site_base_url}/v/{movie_id}"


def build_javdb_app_api(settings: Settings, client: httpx.AsyncClient) -> JavDBAppApi | None:
    """App API client from settings, or ``None`` when disabled / no hosts configured."""

    if not settings.javdb_api_enabled:
        return None
    hosts = tuple(part.strip() for part in settings.javdb_api_hosts.split(",") if part.strip())
    if not hosts:
        return None
    return JavDBAppApi(
        client,
        hosts=hosts,
        site_base_url=settings.javdb_base_url,
        token=settings.javdb_api_token,
    )
