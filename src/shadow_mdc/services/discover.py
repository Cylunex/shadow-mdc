"""Remote catalogue discovery without writing library media rows.

Inspired by miyabi's discover≠library split: browse/search provider lists, project
local Work/asset state, and only create Work when the user explicitly seeds.
"""

from __future__ import annotations

from datetime import date
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
from ..providers.javdb import JavDBProvider

DiscoverState = Literal["not_in_library", "catalog_only", "in_library"]
DiscoverList = Literal["latest", "rankings_daily", "rankings_weekly", "rankings_monthly"]


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
    note: str = (
        "Seeded from discover metadata only; not library media until files are scanned."
    )


_LIST_PATHS: dict[DiscoverList, str] = {
    "latest": "/",
    "rankings_daily": "/rankings?period=daily",
    "rankings_weekly": "/rankings?period=weekly",
    "rankings_monthly": "/rankings?period=monthly",
}


class DiscoverService:
    def __init__(self, providers: ProviderRegistry, javdb: JavDBProvider | None):
        self._providers = providers
        self._javdb = javdb

    async def browse(
        self,
        repo: Repository,
        *,
        provider: str = "javdb",
        list_name: DiscoverList = "latest",
        page: int = 1,
    ) -> DiscoverPage:
        if provider != "javdb" or self._javdb is None:
            raise ValueError(f"browse list is not available for provider: {provider}")
        path = _LIST_PATHS[list_name]
        separator = "&" if "?" in path else "?"
        url = f"{self._javdb.base_url}{path}{separator}page={max(1, page)}"
        html = await self._javdb.fetch_html(url)
        items = self._project(repo, parse_javdb_list(html, self._javdb.base_url))
        return DiscoverPage(provider=provider, list=list_name, page=page, items=tuple(items))

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
    ) -> DiscoverSeedResult:
        record = await self._fetch_record(
            provider=provider,
            external_id=external_id,
            source_url=source_url,
            code=code,
        )
        existing = repo.find_work_by_code(record.code) if record.code else None
        created = existing is None
        work = repo.upsert_provider_record(record, overwrite=False)
        return DiscoverSeedResult(
            work_id=work.id,
            created=created,
            title=work.title,
            primary_code=work.primary_code,
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
        if provider == "javdb" and self._javdb is not None:
            return await self._javdb.magnets(source_url or external_id)
        raise ValueError(f"magnets are not available for provider: {provider}")

    async def _fetch_record(
        self,
        *,
        provider: str,
        external_id: str | None,
        source_url: str | None,
        code: str | None = None,
    ) -> ProviderRecord:
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
        if not batch.records:
            batch = await self._providers.search(hints)
        if not batch.records:
            raise LookupError("no provider records for discover seed/detail")
        preferred = next((item for item in batch.records if item.provider == provider), batch.records[0])
        return preferred

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
        seen.add(source_url)
        external_id = urlparse(source_url).path.rstrip("/").split("/")[-1]
        title_node = node.css_first(".video-title") or node.css_first(".title") or link
        raw_title = title_node.text(separator=" ", strip=True) if title_node is not None else external_id
        code_node = node.css_first("strong") or node.css_first(".uid")
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
