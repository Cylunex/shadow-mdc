"""Remote catalogue discovery without writing library media rows.

Inspired by miyabi's discover≠library split: browse/search provider lists, project
local Work/asset state, and only create Work when the user explicitly seeds.
"""

from __future__ import annotations

import logging
import re
from datetime import date
from pathlib import Path
from typing import Literal
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict
from selectolax.parser import HTMLParser

from ..db.models import Work
from ..db.repository import Repository
from ..domain import IdentityHints, ProviderRecord
from ..enums import ContentFamily, MediaCategory, QueryMode
from ..identity import extract_code
from ..providers.base import ProviderRegistry
from ..providers.html import absolute, parse_date
from ..media.magnets import MagnetLink
from ..providers.fanza import FanzaProvider
from ..providers.javdb import JavDBProvider
from ..providers.javdb_api import JavDBApiMovie, JavDBAppApi
from .r18_dump import R18DumpStore

logger = logging.getLogger(__name__)

DiscoverState = Literal["not_in_library", "catalog_only", "in_library"]
DiscoverList = Literal[
    "latest",
    "rankings_daily",
    "rankings_weekly",
    "rankings_monthly",
    "rankings_daily_uncensored",
    "rankings_weekly_uncensored",
    "rankings_monthly_uncensored",
    "rankings_daily_western",
    "rankings_weekly_western",
    "rankings_monthly_western",
    "rankings_daily_fc2",
    "rankings_weekly_fc2",
    "rankings_monthly_fc2",
    "top250",
]

# JavDB ranking zones; ``rankings_<period>`` (no suffix) stays the censored list.
JAVDB_RANKING_ZONES: tuple[str, ...] = ("censored", "uncensored", "western", "fc2")
# Lists only JavDB can serve (FANZA has no zone split / TOP250).
JAVDB_ONLY_LISTS: frozenset[str] = frozenset(
    {
        f"rankings_{period}_{zone}"
        for period in ("daily", "weekly", "monthly")
        for zone in ("uncensored", "western", "fc2")
    }
    | {"top250"}
)


def javdb_ranking_spec(list_name: str) -> tuple[str, str] | None:
    """``rankings_weekly_fc2`` → ``("fc2", "weekly")``; non-ranking lists → ``None``."""

    if not list_name.startswith("rankings_"):
        return None
    parts = list_name.split("_")
    if len(parts) == 2 and parts[1] in {"daily", "weekly", "monthly"}:
        return "censored", parts[1]
    if len(parts) == 3 and parts[1] in {"daily", "weekly", "monthly"} and parts[2] in JAVDB_RANKING_ZONES:
        return parts[2], parts[1]
    return None


class DiscoverItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    external_id: str
    source_url: str
    code: str | None = None
    title: str
    thumb_url: str | None = None
    release_date: date | None = None
    state: DiscoverState = "not_in_library"
    work_id: str | None = None
    has_local_media: bool = False


class DiscoverDetail(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    item: DiscoverItem
    studio: str | None = None
    actors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    plot: str | None = None
    runtime_seconds: int | None = None


class DiscoverPage(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    list: str | None = None
    query: str | None = None
    page: int
    items: tuple[DiscoverItem, ...]
    # Which transport answered (``javdb_app_api`` / ``javdb_html`` / ``fanza_graphql``…).
    source: str | None = None
    # Why a fallback was used (e.g. the app API error before HTML scraping).
    note: str | None = None




class ProviderSearchHit(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    item: DiscoverItem
    magnets: tuple[MagnetLink, ...] = ()
    magnets_error: str | None = None


class MultiSiteSearchResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    query: str
    code: str | None
    hits: tuple[ProviderSearchHit, ...]
    failures: tuple[str, ...] = ()

class DiscoverSeedResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    created: bool
    title: str
    primary_code: str | None
    # Set when online providers failed and the offline r18 dump built the entry.
    fallback: str | None = None
    note: str = (
        "Seeded from discover metadata only; not library media until files are scanned."
    )


# JavDB moved rankings from ``/rankings?period=…`` (now 404) to
# ``/rankings/movies?p=<daily|weekly|monthly>&t=<censored|uncensored|western|fc2>``.
# Ranking pages are a single fixed list (``page`` is ignored by the site). The HTML
# scrape is the *fallback*; the signed app API (``JavDBAppApi.rankings``) is primary.
_LIST_PATHS: dict[str, str] = {"latest": "/"}
for _period in ("daily", "weekly", "monthly"):
    _LIST_PATHS[f"rankings_{_period}"] = f"/rankings/movies?p={_period}&t=censored"
    for _zone in ("uncensored", "western", "fc2"):
        _LIST_PATHS[f"rankings_{_period}_{_zone}"] = f"/rankings/movies?p={_period}&t={_zone}"
_SINGLE_PAGE_LISTS: frozenset[str] = frozenset(name for name in _LIST_PATHS if name.startswith("rankings_"))


def javdb_list_url(base_url: str, list_name: DiscoverList, page: int = 1) -> str:
    path = _LIST_PATHS.get(list_name)
    if path is None:
        raise ValueError(f"no JavDB web page for list: {list_name}")
    if list_name in _SINGLE_PAGE_LISTS:
        return f"{base_url.rstrip('/')}{path}"
    separator = "&" if "?" in path else "?"
    return f"{base_url.rstrip('/')}{path}{separator}page={max(1, page)}"


def item_from_api_movie(movie: JavDBApiMovie, site_base_url: str) -> DiscoverItem:
    code, _family = extract_code(movie.number) if movie.number else (None, None)
    title = movie.title or movie.number or movie.id
    if code and title.upper().startswith(code.upper()):
        title = title[len(code):].strip(" -") or title
    return DiscoverItem(
        provider="javdb",
        external_id=movie.id,
        source_url=f"{site_base_url.rstrip('/')}/v/{movie.id}",
        code=code,
        title=title,
        # ``cover_url`` is the landscape cover (cards render it with ``contain``).
        thumb_url=movie.cover_url or movie.thumb_url,
        release_date=movie.release_date,
    )


class DiscoverService:
    def __init__(
        self,
        providers: ProviderRegistry,
        javdb: JavDBProvider | None,
        fanza: FanzaProvider | None = None,
        *,
        r18_dump_path: Path | None = None,
        javdb_api: JavDBAppApi | None = None,
    ):
        self._providers = providers
        self._javdb = javdb
        self._fanza = fanza
        self._javdb_api = javdb_api
        self._r18_dump_path = Path(r18_dump_path) if r18_dump_path is not None else None
        self._r18_store: R18DumpStore | None = None

    @property
    def r18_fallback_available(self) -> bool:
        return self._r18_dump_path is not None and self._r18_dump_path.is_file()

    def _r18_fallback_record(self, code: str | None) -> ProviderRecord | None:
        """Build a record from the offline r18 dump (intake create path only)."""

        if not code or not self.r18_fallback_available:
            return None
        parsed, family = extract_code(code)
        if parsed is None or family is not ContentFamily.JAV or parsed.startswith("FC2-"):
            return None
        if self._r18_store is None:
            assert self._r18_dump_path is not None
            try:
                self._r18_store = R18DumpStore(self._r18_dump_path)
            except Exception as exc:  # noqa: BLE001 - degrade to "no fallback"
                logger.warning("r18 dump unavailable: %s", type(exc).__name__)
                return None
        try:
            return self._r18_store.build_record(parsed)
        except Exception as exc:  # noqa: BLE001 - corrupt/locked dump must not break intake
            logger.warning("r18 dump lookup failed for %s: %s", parsed, type(exc).__name__)
            return None

    def close(self) -> None:
        if self._r18_store is not None:
            self._r18_store.close()
            self._r18_store = None

    async def browse(
        self,
        repo: Repository,
        *,
        provider: str = "javdb",
        list_name: DiscoverList = "latest",
        page: int = 1,
    ) -> DiscoverPage:
        if provider == "fanza":
            if self._fanza is None:
                raise ValueError(f"browse list is not available for provider: {provider}")
            ranking = await self._fanza.fetch_ranking(list_name, limit=100, offset=max(0, (page - 1) * 100))
            raw = [
                DiscoverItem(
                    provider="fanza",
                    external_id=item.content_id,
                    source_url=item.source_url,
                    code=item.code,
                    title=item.title,
                    thumb_url=item.thumb_url,
                )
                for item in ranking
            ]
            items = self._project(repo, raw)
            return DiscoverPage(
                provider=provider, list=list_name, page=page, items=tuple(items), source="fanza_graphql"
            )
        if provider != "javdb" or (self._javdb is None and self._javdb_api is None):
            raise ValueError(f"browse list is not available for provider: {provider}")
        spec = javdb_ranking_spec(list_name)
        is_ranking = spec is not None or list_name == "top250"
        if is_ranking and page > 1 and list_name != "top250":
            return DiscoverPage(provider=provider, list=list_name, page=page, items=())
        api_note: str | None = None
        if is_ranking and self._javdb_api is not None:
            try:
                if list_name == "top250":
                    movies = await self._javdb_api.top250(page=page)
                else:
                    assert spec is not None
                    movies = await self._javdb_api.rankings(spec[0], spec[1])
            except Exception as exc:  # noqa: BLE001 - fall back to the HTML scrape
                api_note = f"app API failed: {type(exc).__name__}: {exc}"
                logger.warning("javdb app API %s failed, falling back to HTML: %s", list_name, api_note)
            else:
                if movies:
                    raw = [item_from_api_movie(movie, self._javdb_api.site_base_url) for movie in movies]
                    items = self._project(repo, raw)
                    return DiscoverPage(
                        provider=provider,
                        list=list_name,
                        page=page,
                        items=tuple(items),
                        source="javdb_app_api",
                    )
                api_note = "app API returned an empty list"
        if list_name == "top250":
            raise ValueError(
                "JavDB TOP250 needs the app API with SHADOW_MDC_JAVDB_API_TOKEN"
                + (f" ({api_note})" if api_note else "")
            )
        if self._javdb is None:
            raise LookupError(api_note or f"javdb HTML provider unavailable for {list_name}")
        url = javdb_list_url(self._javdb.base_url, list_name, page)
        html = await self._javdb.fetch_html(url)
        items = self._project(repo, parse_javdb_list(html, self._javdb.base_url))
        return DiscoverPage(
            provider=provider,
            list=list_name,
            page=page,
            items=tuple(items),
            source="javdb_html",
            note=api_note,
        )

    async def search(
        self,
        repo: Repository,
        *,
        query: str,
        provider: str = "javdb",
        page: int = 1,
    ) -> DiscoverPage:
        query = query.strip()
        if not query:
            raise ValueError("query is required")
        if provider == "javdb" and self._javdb is not None:
            url = f"{self._javdb.base_url}/search?q={quote(query)}&f=all&page={max(1, page)}"
            html = await self._javdb.fetch_html(url, params={"locale": "zh"})
            items = self._project(repo, parse_javdb_list(html, self._javdb.base_url))
            return DiscoverPage(provider=provider, query=query, page=page, items=tuple(items))
        code, family = extract_code(query)
        if code:
            hints = IdentityHints(
                term=code,
                mode=QueryMode.CODE,
                family=family,
                category=MediaCategory.JAPAN if family is ContentFamily.JAV else MediaCategory.OTHER,
                code=code,
            )
        else:
            hints = IdentityHints(
                term=query,
                mode=QueryMode.TEXT,
                family=ContentFamily.UNKNOWN,
                category=MediaCategory.OTHER,
                title=query,
            )
        provider_ids = None if provider == "all" else (provider,)
        batch = await self._providers.search(hints, provider_ids=provider_ids)
        raw = [_item_from_record(record) for record in batch.records]
        return DiscoverPage(
            provider=provider,
            query=query,
            page=page,
            items=tuple(self._project(repo, raw)),
        )

    async def detail(self, repo: Repository, *, provider: str, external_id: str) -> DiscoverDetail:
        record = await self._fetch_record(provider=provider, external_id=external_id, source_url=None)
        item = self._project(repo, [_item_from_record(record)])[0]
        return DiscoverDetail(
            item=item,
            studio=record.studio,
            actors=record.actors,
            tags=record.tags,
            plot=record.plot,
            runtime_seconds=record.runtime_seconds,
        )

    async def seed(
        self,
        repo: Repository,
        *,
        provider: str,
        external_id: str | None = None,
        source_url: str | None = None,
        code: str | None = None,
        allow_r18_fallback: bool = True,
    ) -> DiscoverSeedResult:
        fallback: str | None = None
        try:
            record = await self._fetch_record(
                provider=provider,
                external_id=external_id,
                source_url=source_url,
                code=code,
            )
        except LookupError:
            dump_record = self._r18_fallback_record(code) if allow_r18_fallback else None
            if dump_record is None:
                raise
            record = dump_record
            fallback = "r18dump"
        existing = repo.find_work_by_code(record.code) if record.code else None
        created = existing is None
        work = repo.upsert_provider_record(record, overwrite=False)
        return DiscoverSeedResult(
            work_id=work.id,
            created=created,
            title=work.title,
            primary_code=work.primary_code,
            fallback=fallback,
        )

    async def multi_site_search(
        self,
        repo: Repository,
        *,
        query: str,
        include_magnets: bool = True,
    ) -> MultiSiteSearchResult:
        """Search all eligible providers by code/text and optionally attach magnet lists."""

        query = query.strip()
        if not query:
            raise ValueError("query is required")
        code, family = extract_code(query)
        if code:
            hints = IdentityHints(
                term=code,
                mode=QueryMode.CODE,
                family=family,
                category=MediaCategory.JAPAN if family is ContentFamily.JAV else MediaCategory.OTHER,
                code=code,
            )
        else:
            hints = IdentityHints(
                term=query,
                mode=QueryMode.TEXT,
                family=ContentFamily.UNKNOWN,
                category=MediaCategory.OTHER,
                title=query,
            )
        batch = await self._providers.search(hints)
        failures = [f"{item.provider}: {item.reason}: {item.detail}" for item in batch.failures]
        hits: list[ProviderSearchHit] = []
        for record in batch.records:
            item = self._project(repo, [_item_from_record(record)])[0]
            magnets: tuple[MagnetLink, ...] = ()
            magnets_error: str | None = None
            if include_magnets and record.provider == "javdb" and self._javdb is not None:
                try:
                    magnets = await self._javdb.magnets(record.source_url or record.external_id)
                except Exception as exc:  # noqa: BLE001 - surface per-source
                    magnets_error = f"{type(exc).__name__}: {exc}"
            hits.append(
                ProviderSearchHit(
                    provider=record.provider,
                    item=item,
                    magnets=magnets,
                    magnets_error=magnets_error,
                )
            )
        return MultiSiteSearchResult(query=query, code=code, hits=tuple(hits), failures=tuple(failures))

    async def list_magnets(self, *, provider: str, external_id: str, source_url: str | None = None) -> tuple[MagnetLink, ...]:
        if provider != "javdb" or (self._javdb is None and self._javdb_api is None):
            raise ValueError(f"magnets are not available for provider: {provider}")
        html_error: Exception | None = None
        html_magnets: tuple[MagnetLink, ...] = ()
        if self._javdb is not None:
            try:
                html_magnets = await self._javdb.magnets(source_url or external_id)
            except Exception as exc:  # noqa: BLE001 - app API fallback below
                html_error = exc
        if html_magnets or self._javdb_api is None:
            if html_error is not None:
                raise html_error
            return html_magnets
        movie_id = external_id
        if movie_id.startswith(("http://", "https://")):
            movie_id = javdb_movie_id_from_url(movie_id) or ""
        if not movie_id and source_url:
            movie_id = javdb_movie_id_from_url(source_url) or ""
        if not movie_id:
            if html_error is not None:
                raise html_error
            return ()
        try:
            # App API carries explicit cnsub/hd flags and file counts.
            return await self._javdb_api.magnets(movie_id)
        except Exception:
            if html_error is not None:
                raise html_error from None
            raise

    async def _fetch_record(
        self,
        *,
        provider: str,
        external_id: str | None,
        source_url: str | None,
        code: str | None = None,
    ) -> ProviderRecord:
        # FANZA detail pages are often geo/age gated from HTML; prefer code/cid.
        if provider == "fanza" and code:
            source_url = None
        if source_url:
            hints = IdentityHints(
                term=source_url,
                mode=QueryMode.URL,
                family=ContentFamily.UNKNOWN,
                category=MediaCategory.OTHER,
                source_url=source_url,
            )
        elif code:
            parsed, family = extract_code(code)
            if parsed is None:
                raise ValueError("code is not a supported media identity")
            hints = IdentityHints(
                term=parsed,
                mode=QueryMode.CODE,
                family=family,
                category=MediaCategory.JAPAN,
                code=parsed,
            )
        elif external_id and provider == "javdb" and self._javdb is not None:
            url = f"{self._javdb.base_url}/v/{external_id}"
            hints = IdentityHints(
                term=url,
                mode=QueryMode.URL,
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                source_url=url,
                external_ids={"javdb": external_id},
            )
        elif external_id:
            hints = IdentityHints(
                term=external_id,
                mode=QueryMode.EXTERNAL_ID,
                family=ContentFamily.UNKNOWN,
                category=MediaCategory.OTHER,
                external_ids={provider: external_id},
            )
        else:
            raise ValueError("external_id, source_url, or code is required")
        batch = await self._providers.search(hints, provider_ids=(provider,))
        if not batch.records and provider == "javdb":
            api_record = await self._javdb_api_record(external_id=external_id, source_url=source_url)
            if api_record is not None:
                return api_record
        if not batch.records:
            batch = await self._providers.search(hints)
        if not batch.records:
            raise LookupError("no provider records for discover seed/detail")
        preferred = next((item for item in batch.records if item.provider == provider), batch.records[0])
        return preferred

    async def _javdb_api_record(
        self, *, external_id: str | None, source_url: str | None
    ) -> ProviderRecord | None:
        """JavDB detail via the app API when the HTML page fails (blocked / layout drift)."""

        if self._javdb_api is None:
            return None
        movie_id = external_id
        if not movie_id and source_url:
            movie_id = javdb_movie_id_from_url(source_url)
        if not movie_id:
            return None
        try:
            return await self._javdb_api.movie_detail(movie_id)
        except Exception as exc:  # noqa: BLE001 - caller falls back further
            logger.warning("javdb app API detail %s failed: %s: %s", movie_id, type(exc).__name__, exc)
            return None

    def _project(self, repo: Repository, items: list[DiscoverItem]) -> list[DiscoverItem]:
        projected: list[DiscoverItem] = []
        for item in items:
            work = self._match_work(repo, item)
            if work is None:
                projected.append(item)
                continue
            has_media = bool(repo.list_assets_for_work(work.id))
            projected.append(
                item.model_copy(
                    update={
                        "state": "in_library" if has_media else "catalog_only",
                        "work_id": work.id,
                        "has_local_media": has_media,
                    }
                )
            )
        return projected

    def _match_work(self, repo: Repository, item: DiscoverItem) -> Work | None:
        if item.code:
            found = repo.find_work_by_code(item.code)
            if found is not None:
                return found
        return repo.find_work_by_provider_identity(item.provider, item.external_id)

    def _parse_javdb_list(self, html: str, base_url: str) -> list[DiscoverItem]:
        return parse_javdb_list(html, base_url)


_JAVDB_MOVIE_PATH = re.compile(r"^/v/([A-Za-z0-9]+)/?$")


def javdb_movie_id_from_url(value: str) -> str | None:
    """Movie id only from an explicit ``/v/<id>`` detail path (JHS ``extractJavDbMovieId``)."""

    try:
        path = urlparse(value).path
    except ValueError:
        return None
    matched = _JAVDB_MOVIE_PATH.match(path)
    return matched.group(1) if matched else None


def parse_javdb_list(html: str, base_url: str) -> list[DiscoverItem]:
    root = HTMLParser(html)
    items: list[DiscoverItem] = []
    seen: set[str] = set()
    for node in root.css(".movie-list .item, .grid .item, .item"):
        link = node.css_first("a[href*='/v/']")
        if link is None:
            continue
        href = link.attributes.get("href")
        if not href or "/v/" not in href:
            continue
        source_url = absolute(base_url, href)
        if source_url is None or source_url in seen:
            continue
        external_id = javdb_movie_id_from_url(source_url)
        if external_id is None:
            # Review / list sub-pages (``/v/<id>/reviews``) are not movie cards.
            continue
        seen.add(source_url)
        title_node = node.css_first(".video-title") or node.css_first(".title") or link
        raw_title = title_node.text(separator=" ", strip=True) if title_node is not None else external_id
        # Card layout (JHS list-item-reader): the code is ``.video-title strong``;
        # a bare first ``strong`` can be a score/badge on newer cards.
        code_node = (
            node.css_first(".video-title strong")
            or node.css_first(".uid")
            or node.css_first("strong")
        )
        code_text = code_node.text(strip=True) if code_node is not None else None
        parsed_code, _family = extract_code(code_text or raw_title)
        img = node.css_first("img")
        thumb = None
        if img is not None:
            thumb = absolute(
                base_url,
                img.attributes.get("src") or img.attributes.get("data-src"),
            )
        meta = node.css_first(".meta")
        release = parse_date(meta.text(separator=" ", strip=True)) if meta is not None else None
        items.append(
            DiscoverItem(
                provider="javdb",
                external_id=external_id,
                source_url=source_url,
                code=parsed_code,
                title=raw_title or external_id,
                thumb_url=thumb,
                release_date=release,
            )
        )
    return items


def _item_from_record(record: ProviderRecord) -> DiscoverItem:
    thumb = next((str(item.url) for item in record.artwork), None)
    return DiscoverItem(
        provider=record.provider,
        external_id=record.external_id,
        source_url=record.source_url or f"provider://{record.provider}/{record.external_id}",
        code=record.code,
        title=record.title,
        thumb_url=thumb,
        release_date=record.release_date,
    )
