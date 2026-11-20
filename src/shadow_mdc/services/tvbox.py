"""TVBox configuration normalization, durable site state and on-demand CMS browsing."""

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections import Counter
from datetime import UTC, datetime
from urllib.parse import parse_qsl, urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from selectolax.parser import HTMLParser
from sqlalchemy import select

from ..db.models import TvBoxPack, TvBoxRevision, TvBoxSite, utc_now
from ..db.repository import Database
from .playback import type_name_banned
from .tvbox_http import TvBoxError, download, http_url, playback_mime

logger = logging.getLogger(__name__)

MAX_SITES = 100
MAX_CONFIGS = 32
MAX_PAGE = 500
JsonRecord = dict[str, object]
# Match strings before comments and commas, so URLs and string literals are kept intact.
_JSONC = re.compile(r'"(?:\\.|[^"\\])*"|//[^\r\n]*|/\*[\s\S]*?\*/')
_TRAILING_COMMA = re.compile(r'"(?:\\.|[^"\\])*"|,(?=\s*[}\]])')
_INLINE_HEADERS = {"referer": "Referer", "origin": "Origin", "user-agent": "User-Agent"}


def identity(*parts: str) -> str:
    return hashlib.sha256(json.dumps(parts, ensure_ascii=False).encode()).hexdigest()[:32]


def config_json(content: str) -> JsonRecord:
    stripped = _JSONC.sub(lambda m: m[0] if m[0].startswith('"') else " ", content.lstrip("\ufeff"))
    stripped = _TRAILING_COMMA.sub(lambda m: m[0] if m[0].startswith('"') else "", stripped)
    try:
        value = json.loads(stripped)
    except (ValueError, RecursionError) as exc:
        raise TvBoxError("配置不是有效的 JSON/JSONC") from exc
    if not isinstance(value, dict):
        raise TvBoxError("需要 TVBox 配置对象")
    return value


class Category(BaseModel):
    id: str = Field(validation_alias="type_id", min_length=1, max_length=100)
    name: str = Field(validation_alias="type_name", min_length=1, max_length=200)
    model_config = ConfigDict(coerce_numbers_to_str=True, validate_by_name=True)


class Vod(BaseModel):
    model_config = ConfigDict(coerce_numbers_to_str=True)
    vod_id: str = Field(min_length=1, max_length=200)
    vod_name: str = Field(min_length=1, max_length=400)
    type_id: str = ""
    type_name: str = ""
    vod_pic: str = ""
    vod_content: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""

    @field_validator(
        "type_id", "type_name", "vod_pic", "vod_content", "vod_play_from", "vod_play_url", mode="before"
    )
    @classmethod
    def empty_if_null(cls, value: object) -> object:
        return "" if value is None else value


class SiteInput(BaseModel):
    name: str
    endpoint: str
    allow_http: bool


class SiteStatus(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    enabled: bool
    checked_at: datetime | None = None
    failure: str | None = None
    active_revision: str | None = None
    previous_revision: str | None = None
    priority: int = 0


class ImportResult(BaseModel):
    sites: list[SiteStatus] = Field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)


class ImportJob(BaseModel):
    id: str
    result: ImportResult | None = None


class Card(BaseModel):
    id: str
    site_id: str
    title: str
    overview: str = ""
    image: bool = False


class Episode(BaseModel):
    id: str
    title: str
    line: str


class Detail(Card):
    episodes: list[Episode]


class Tracks(BaseModel):
    entry: Episode
    url: str
    headers: dict[str, str]


class Playback(BaseModel):
    url: str
    mime_type: str
    headers: dict[str, str]


class Page(BaseModel):
    items: list[Card]
    next_page: int | None = None
    failures: list[str] = Field(default_factory=list)


class SiteCatalog:
    def __init__(self, site: SiteInput, categories: list[Category]) -> None:
        self.site = site
        self.categories = categories
        self.allowed = {c.id for c in categories}

    async def fetch(self, **params: str | int) -> JsonRecord:
        url = httpx.URL(self.site.endpoint)
        for key in ("ac", "pg", "wd", "t", "ids"):
            url = url.copy_remove_param(key)
        response = await download(str(url.copy_merge_params(params)), allow_http=self.site.allow_http)
        return config_json(response.body.decode("utf-8", errors="replace"))

    def accepts(self, row: Vod) -> bool:
        return row.type_id in self.allowed and not type_name_banned(row.type_name)

    async def detail(self, item_id: str) -> Vod:
        if not item_id or len(item_id) > 200 or "," in item_id:
            raise TvBoxError("影片标识无效")
        payload = await self.fetch(ac="detail", ids=item_id)
        row = next((row for row in _records(payload) if row.vod_id == item_id and self.accepts(row)), None)
        if row is None:
            raise TvBoxError("影片不存在或所在分类不可用")
        return row

    def tracks(self, row: Vod) -> list[Tracks]:
        names = row.vod_play_from.split("$$$")
        tracks: list[Tracks] = []
        for index, group in enumerate(row.vod_play_url.split("$$$")[:32]):
            label = names[index].strip() if index < len(names) else ""
            for entry in group.split("#")[:500]:
                title, separator, locator = entry.strip().partition("$")
                if not separator or title.startswith(("http://", "https://")):
                    title, locator = "正片", entry.strip()
                if not locator or not title:
                    continue
                source, _, raw_headers = locator.partition("|")
                try:
                    url = http_url(
                        urljoin(self.site.endpoint, source.strip()), allow_http=self.site.allow_http
                    )
                except ValueError:
                    continue
                headers = {
                    _INLINE_HEADERS[key.lower()]: value
                    for key, value in parse_qsl(raw_headers)
                    if key.lower() in _INLINE_HEADERS
                    and value.isascii()
                    and value.isprintable()
                    and len(value) < 1024
                }
                # A source label is stable across reordering and signed URL refreshes.
                # Unnamed or ambiguous lines require a path identity.
                hint = label if label and names.count(label) == 1 else f"{label}:{url.host}{url.path}"
                item = Episode(id=identity(row.vod_id, title, hint), title=title[:200], line=label[:200])
                tracks.append(Tracks(entry=item, url=str(url), headers=headers))
        counts = Counter(track.entry.id for track in tracks)
        for track in tracks:
            if counts[track.entry.id] > 1:
                url = httpx.URL(track.url)
                track.entry.id = identity(
                    track.entry.id, url.host, url.path, url.query.decode(errors="replace")
                )
        return list({track.entry.id: track for track in tracks}.values())


def _records(payload: JsonRecord) -> list[Vod]:
    rows = payload.get("list")
    if not isinstance(rows, list):
        return []
    result: list[Vod] = []
    for row in rows[:120]:
        try:
            result.append(Vod.model_validate(row))
        except ValidationError:
            continue
    return result


def _card(site_id: str, row: Vod) -> Card:
    key = base64.urlsafe_b64encode(row.vod_id.encode()).decode().rstrip("=")
    return Card(id=key, site_id=site_id, title=row.vod_name, image=bool(row.vod_pic))


def _item_id(key: str) -> str:
    if len(key) > 1100 or not re.fullmatch(r"[a-zA-Z0-9_-]+", key):
        raise LookupError("影片标识无效")
    try:
        return base64.b64decode(key + "=" * (-len(key) % 4), altchars=b"-_", validate=True).decode()
    except ValueError as exc:
        raise LookupError("影片标识无效") from exc


def _integer(value: object) -> int:
    try:
        return int(str(value))
    except ValueError:
        return 0


async def _discover(
    url: str | None,
    content: str | None,
    allow_http: bool,
    *,
    base_url: str | None = None,
) -> tuple[list[SiteInput], ImportResult]:
    result = ImportResult()
    found: dict[str, SiteInput] = {}
    pending = [(url, content, 0)]
    seen: set[str] = set()
    while pending and len(seen) < MAX_CONFIGS and len(found) < MAX_SITES:
        source, inline, depth = pending.pop(0)
        try:
            if source:
                source = str(http_url(source, allow_http=allow_http))
                if source in seen:
                    continue
                seen.add(source)
            if inline is None:
                if source is None:
                    raise TvBoxError("请提供配置地址或文件")
                fetched = await download(source, allow_http=allow_http)
                inline = fetched.body.decode("utf-8", errors="replace")
                source = fetched.url
            parsed = config_json(inline)
        except TvBoxError as exc:
            result.warnings.append(str(exc))
            continue
        base = source or base_url or ""
        if source and ("class" in parsed or "list" in parsed):
            found[source] = SiteInput(name=httpx.URL(source).host, endpoint=source, allow_http=allow_http)
            continue
        sites = parsed.get("sites")
        if isinstance(sites, list):
            for row in sites:
                if len(found) >= MAX_SITES:
                    break
                if not isinstance(row, dict) or row.get("type") not in (1, "1"):
                    result.skipped += 1
                    continue
                try:
                    endpoint = str(http_url(urljoin(base, str(row["api"])), allow_http=allow_http))
                except (KeyError, ValueError):
                    result.skipped += 1
                    continue
                name = str(row.get("name") or row.get("key") or httpx.URL(endpoint).host)
                found.setdefault(
                    endpoint, SiteInput(name=name[:200], endpoint=endpoint, allow_http=allow_http)
                )
        nested = parsed.get("urls")
        if depth < 3 and isinstance(nested, list):
            for row in nested[:MAX_CONFIGS]:
                if isinstance(row, dict) and isinstance(row.get("url"), str):
                    try:
                        pending.append((urljoin(base, row["url"]), None, depth + 1))
                    except ValueError:
                        result.warnings.append("多仓子配置地址无效")
        lives = parsed.get("lives")
        if isinstance(lives, list):
            result.skipped += len(lives)
    if pending:
        result.warnings.append("多仓目录达到本次导入上限")
    if not found and not result.warnings:
        result.warnings.append("配置里没有可测试的 type 1 JSON 站点")
    return list(found.values()), result


class TvBoxService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._updates = asyncio.Lock()
        self._requests = asyncio.Semaphore(8)
        self._imports: dict[str, asyncio.Task[ImportResult]] = {}

    def submit_import(
        self,
        url: str | None,
        content: str | None,
        allow_http: bool,
        base_url: str | None,
    ) -> ImportJob:
        key = identity(url or "", content or "", str(allow_http), base_url or "")
        previous = self._imports.get(key)
        if previous and not previous.done():
            return ImportJob(id=key)
        if sum(not task.done() for task in self._imports.values()) >= 4:
            raise TvBoxError("已有导入在处理")
        for job_id, task in list(self._imports.items())[:-15]:
            if task.done():
                self._imports.pop(job_id)
        self._imports[key] = asyncio.create_task(self._run_import(url, content, allow_http, base_url))
        return ImportJob(id=key)

    async def _run_import(
        self,
        url: str | None,
        content: str | None,
        allow_http: bool,
        base_url: str | None,
    ) -> ImportResult:
        try:
            return await self.import_config(url, content, allow_http, base_url=base_url)
        except Exception as exc:
            logger.warning("TVBox import failed: %s", type(exc).__name__)
            return ImportResult(warnings=["导入中断。请重试"])

    def import_job(self, key: str) -> ImportJob:
        task = self._imports.get(key)
        if not task or task.cancelled():
            raise LookupError("导入任务不存在或已中断")
        return ImportJob(id=key, result=task.result() if task.done() else None)

    async def close(self) -> None:
        for task in self._imports.values():
            task.cancel()
        await asyncio.gather(*self._imports.values(), return_exceptions=True)

    def sites(self, *, all_sites: bool = False) -> list[SiteStatus]:
        with self.database.session() as session:
            query = select(TvBoxSite).order_by(TvBoxSite.priority.desc(), TvBoxSite.name, TvBoxSite.id)
            if not all_sites:
                query = query.where(TvBoxSite.enabled, TvBoxSite.active_revision.is_not(None))
            return [SiteStatus.model_validate(site) for site in session.scalars(query)]

    def _site(self, site_id: str) -> TvBoxSite:
        with self.database.session() as session:
            site = session.get(TvBoxSite, site_id)
            if site is None:
                raise LookupError("站点不存在")
            return site

    def _catalog(self, site_id: str) -> SiteCatalog:
        with self.database.session() as session:
            site = session.get(TvBoxSite, site_id)
            revision = (
                session.get(TvBoxRevision, site.active_revision) if site and site.active_revision else None
            )
            if site is None or not site.enabled or revision is None:
                raise LookupError("站点不可用")
            source = SiteInput(name=site.name, endpoint=site.endpoint, allow_http=site.allow_http)
            return SiteCatalog(source, [Category.model_validate(c) for c in revision.categories])

    async def _check(self, source: SiteInput) -> list[Category]:
        async with self._requests, asyncio.timeout(22):
            payload = await SiteCatalog(source, []).fetch()
            candidates = payload.get("class")
            categories: list[Category] = []
            if isinstance(candidates, list):
                for row in candidates[:200]:
                    try:
                        category = Category.model_validate(row)
                    except ValidationError:
                        continue
                    if category.name.strip() and not type_name_banned(category.name):
                        categories.append(category)
            if not categories:
                raise TvBoxError("上游未返回可用分类")
            catalog = SiteCatalog(source, categories)
            rows = [row for row in _records(payload) if catalog.accepts(row)]
            if not rows:
                rows = _records(await catalog.fetch(ac="detail", t=categories[0].id, pg=1))
            for row in [row for row in rows if catalog.accepts(row)][:2]:
                try:
                    detail = await catalog.detail(row.vod_id)
                except TvBoxError:
                    continue
                for track in catalog.tracks(detail)[:2]:
                    try:
                        await playback_mime(track.url, allow_http=source.allow_http, headers=track.headers)
                    except TvBoxError:
                        continue
                    return categories
            raise TvBoxError("抽样影片未找到通过测试的线路")

    async def _refresh(self, source: SiteInput) -> SiteStatus:
        site_id = identity(source.endpoint)
        with self.database.session() as session:
            existing = session.get(TvBoxSite, site_id)
            baseline = existing.active_revision if existing else None
        try:
            categories = await self._check(source)
            failure = None
        except TvBoxError as exc:
            categories, failure = [], str(exc)
        except TimeoutError:
            categories, failure = [], "站点测试超时"
        with self.database.session() as session:
            site = session.get(TvBoxSite, site_id)
            if site is None:
                site = TvBoxSite(
                    id=site_id,
                    name=source.name,
                    endpoint=source.endpoint,
                    allow_http=source.allow_http,
                    enabled=True,
                    priority=0,
                )
                session.add(site)
                session.flush()
            site.checked_at = utc_now()
            site.failure = failure
            if categories and site.active_revision == baseline:
                data = [category.model_dump() for category in categories]
                revision = identity(site_id, json.dumps(data, sort_keys=True))
                if session.get(TvBoxRevision, revision) is None:
                    session.add(TvBoxRevision(id=revision, site_id=site_id, categories=data))
                if site.active_revision != revision:
                    site.previous_revision = site.active_revision
                    site.active_revision = revision
                site.allow_http = source.allow_http
            return SiteStatus.model_validate(site)

    async def import_config(
        self,
        url: str | None,
        content: str | None,
        allow_http: bool,
        *,
        base_url: str | None = None,
    ) -> ImportResult:
        async with self._updates:
            sites, result = await _discover(url, content, allow_http, base_url=base_url)
            result.sites = list(await asyncio.gather(*(self._refresh(site) for site in sites)))
            if result.sites:
                pack_id = identity(url or content or "")
                with self.database.session() as session:
                    old = session.get(TvBoxPack, pack_id)
                    ids = [site.id for site in result.sites]
                    if old and result.warnings:
                        ids = list(dict.fromkeys(old.site_ids + ids))
                    session.merge(
                        TvBoxPack(
                            id=pack_id,
                            url=url,
                            allow_http=allow_http,
                            site_ids=ids,
                            checked_at=utc_now(),
                        )
                    )
                    if old:
                        others = session.scalars(select(TvBoxPack).where(TvBoxPack.id != pack_id))
                        retained = {site for pack in others for site in pack.site_ids}
                        for missing in set(old.site_ids) - set(ids) - retained:
                            site = session.get(TvBoxSite, missing)
                            if site:
                                site.active_revision = None
            elif url:
                with self.database.session() as session:
                    pack = session.get(TvBoxPack, identity(url))
                    if pack:
                        pack.checked_at = utc_now()
            return result

    async def schedule(self) -> None:
        while True:
            await asyncio.sleep(60)
            with self.database.session() as session:
                packs = [
                    (pack.url, pack.allow_http)
                    for pack in session.scalars(select(TvBoxPack))
                    if pack.url and (utc_now() - pack.checked_at.replace(tzinfo=UTC)).total_seconds() >= 86400
                ]
            for url, allow_http in packs:
                await self._run_import(url, None, allow_http, None)
            await self._check_inline_sites()

    async def _check_inline_sites(self) -> None:
        async with self._updates:
            with self.database.session() as session:
                # File imports have no remote manifest, but their CMS endpoints can still be checked.
                inline = {
                    site_id
                    for pack in session.scalars(select(TvBoxPack).where(TvBoxPack.url.is_(None)))
                    for site_id in pack.site_ids
                }
                now = utc_now()
                sites = [
                    SiteInput(name=site.name, endpoint=site.endpoint, allow_http=site.allow_http)
                    for site in session.scalars(select(TvBoxSite).where(TvBoxSite.enabled))
                    if site.id in inline
                    and (
                        site.checked_at is None
                        or (now - site.checked_at.replace(tzinfo=UTC)).total_seconds() >= 86400
                    )
                ]
            for site in sites[:8]:
                try:
                    await self._refresh(site)
                except Exception as exc:
                    logger.warning("TVBox health check failed: %s", type(exc).__name__)

    async def refresh(self, site_id: str) -> SiteStatus:
        async with self._updates:
            site = self._site(site_id)
            return await self._refresh(
                SiteInput(name=site.name, endpoint=site.endpoint, allow_http=site.allow_http)
            )

    def update(
        self,
        site_id: str,
        *,
        enabled: bool | None,
        name: str | None,
        priority: int | None,
        rollback: bool,
    ) -> SiteStatus:
        with self.database.session() as session:
            site = session.get(TvBoxSite, site_id)
            if site is None:
                raise LookupError("站点不存在")
            if rollback:
                if not site.previous_revision:
                    raise TvBoxError("没有上一可用修订")
                site.active_revision, site.previous_revision = site.previous_revision, site.active_revision
            if enabled is not None:
                site.enabled = enabled
            if name is not None:
                site.name = name
            if priority is not None:
                site.priority = priority
            return SiteStatus.model_validate(site)

    def categories(self, site_id: str) -> list[Category]:
        return self._catalog(site_id).categories

    async def page(self, site_id: str, category: str | None, query: str, page: int) -> Page:
        catalog = self._catalog(site_id)
        params: dict[str, str | int] = {"ac": "detail", "pg": page}
        if category:
            if category not in catalog.allowed:
                raise LookupError("分类不可用")
            params["t"] = category
        if query:
            params["wd"] = query
        async with self._requests:
            payload = await catalog.fetch(**params)
        current = self._catalog(site_id)
        return Page(
            items=[_card(site_id, row) for row in _records(payload) if current.accepts(row)],
            next_page=page + 1 if page < min(MAX_PAGE, _integer(payload.get("pagecount"))) else None,
        )

    async def search(self, query: str, page: int) -> Page:
        sites = self.sites()[:MAX_SITES]

        async def find(site: SiteStatus) -> Page:
            try:
                async with asyncio.timeout(8):
                    return await self.page(site.id, None, query, page)
            except (TvBoxError, LookupError, TimeoutError):
                return Page(items=[], failures=[site.name])

        results = await asyncio.gather(*(find(site) for site in sites))
        return Page(
            items=[row for result in results for row in result.items],
            failures=[failure for result in results for failure in result.failures],
            next_page=page + 1 if any(result.next_page for result in results) else None,
        )

    async def detail(self, site_id: str, item_id: str) -> Detail:
        catalog = self._catalog(site_id)
        async with self._requests:
            row = await catalog.detail(_item_id(item_id))
        current = self._catalog(site_id)
        if not current.accepts(row):
            raise LookupError("分类已变更")
        return Detail(
            **_card(site_id, row).model_dump(),
            episodes=[track.entry for track in current.tracks(row)],
        ).model_copy(update={"overview": HTMLParser(row.vod_content).text(separator=" ", strip=True)[:4000]})

    async def resolve(self, site_id: str, item_id: str, episode_id: str) -> Playback:
        catalog = self._catalog(site_id)
        async with self._requests:
            row = await catalog.detail(_item_id(item_id))
            track = next((track for track in catalog.tracks(row) if track.entry.id == episode_id), None)
            if track is None:
                raise LookupError("分集或线路已变更")
            mime = await playback_mime(track.url, allow_http=catalog.site.allow_http, headers=track.headers)
        if not self._catalog(site_id).accepts(row):
            raise LookupError("分类已变更")
        return Playback(url=track.url, mime_type=mime, headers=track.headers)

    async def image(self, site_id: str, item_id: str) -> tuple[bytes, str]:
        catalog = self._catalog(site_id)
        async with self._requests:
            row = await catalog.detail(_item_id(item_id))
            if not row.vod_pic:
                raise LookupError("没有封面")
            response = await download(
                urljoin(catalog.site.endpoint, row.vod_pic), allow_http=catalog.site.allow_http
            )
        if not self._catalog(site_id).accepts(row):
            raise LookupError("分类已变更")
        if response.mime in ("image/jpeg", "image/png", "image/webp", "image/gif"):
            return response.body, response.mime
        raise LookupError("封面格式不受支持")
