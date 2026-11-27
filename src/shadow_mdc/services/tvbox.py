"""TVBox configuration normalization, durable site state and on-demand CMS browsing."""

import asyncio
import base64
import hashlib
import json
import logging
import re
from collections import Counter, OrderedDict
from datetime import UTC, datetime
from pathlib import Path
from time import monotonic
from typing import Annotated
from urllib.parse import parse_qsl, urljoin

import httpx
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator
from selectolax.parser import HTMLParser
from sqlalchemy import select

from ..db.models import TvBoxPack, TvBoxRevision, TvBoxSite, utc_now
from ..db.repository import Database
from .playback import type_name_banned
from .tvbox_http import TvBoxError, download, http_url, playback_mime
from .tvbox_relay import FORMAT as RELAY_TRANSFER_FORMAT
from .tvbox_relay import RelayTransfer, archive_transfer, prepare_transfer, relay_config
from .tvbox_stream import BrowserStreams
from .tvbox_xml import normalize_xml

logger = logging.getLogger(__name__)

MAX_SITES = 100
MAX_CONFIGS = 32
MAX_PAGE = 500
POSTER_CACHE_BYTES = 32 * 1024 * 1024
POSTER_CACHE_ENTRIES = 128
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
    vod_year: str = ""
    vod_remarks: str = ""
    vod_content: str = ""
    vod_play_from: str = ""
    vod_play_url: str = ""

    @field_validator(
        "type_id",
        "type_name",
        "vod_pic",
        "vod_content",
        "vod_play_from",
        "vod_play_url",
        "vod_year",
        "vod_remarks",
        mode="before",
    )
    @classmethod
    def empty_if_null(cls, value: object) -> object:
        return "" if value is None else value


class SiteInput(BaseModel):
    protocol_type: int = 1
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
    hidden: bool = False
    pinned: bool = False


class PackView(BaseModel):
    id: str
    label: str
    site_count: int
    checked_at: datetime
    sync_minutes: int
    remote: bool


class RevisionView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    created_at: datetime
    categories: list[Category]


class ImportResult(BaseModel):
    sites: list[SiteStatus] = Field(default_factory=list)
    skipped: int = 0
    warnings: list[str] = Field(default_factory=list)
    migrated: dict[str, int] = Field(default_factory=dict)


class ImportJob(BaseModel):
    id: str
    result: ImportResult | None = None


class Card(BaseModel):
    id: str
    site_id: str
    title: str
    overview: str = ""
    image: bool = False
    year: str = ""
    remarks: str = ""


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
    def __init__(self, site: SiteInput, categories: list[Category], proxy: httpx.URL | None = None) -> None:
        self.site = site
        self.categories = categories
        self.allowed = {c.id for c in categories}
        self.proxy = proxy

    async def fetch(self, **params: str | int) -> JsonRecord:
        url = httpx.URL(self.site.endpoint)
        for key in ("ac", "pg", "wd", "t", "ids"):
            url = url.copy_remove_param(key)
        if self.site.protocol_type == 0 and params.get("ac") == "detail":
            params["ac"] = "videolist"
        response = await download(
            str(url.copy_merge_params(params)), allow_http=self.site.allow_http, proxy=self.proxy,
        )
        if self.site.protocol_type == 0:
            return normalize_xml(response.body)
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
    return Card(
        id=key,
        site_id=site_id,
        title=row.vod_name,
        image=bool(row.vod_pic),
        year=row.vod_year[:20],
        remarks=row.vod_remarks[:100],
    )


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
            if source and inline.lstrip("\ufeff \r\n\t").startswith(("<rss", "<?xml")):
                normalize_xml(inline.encode("utf-8"))
                found[source] = SiteInput(
                    name=httpx.URL(source).host,
                    endpoint=source,
                    allow_http=allow_http,
                    protocol_type=0,
                )
                continue
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
                if not isinstance(row, dict) or str(row.get("type")) not in ("0", "1"):
                    result.skipped += 1
                    continue
                try:
                    endpoint = str(http_url(urljoin(base, str(row["api"])), allow_http=allow_http))
                except (KeyError, ValueError):
                    result.skipped += 1
                    continue
                name = str(row.get("name") or row.get("key") or httpx.URL(endpoint).host)
                found.setdefault(
                    endpoint,
                    SiteInput(
                        name=name[:200],
                        endpoint=endpoint,
                        allow_http=allow_http,
                        protocol_type=int(row["type"]),
                    ),
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
        result.warnings.append("配置里没有可测试的 CMS 站点")
    return list(found.values()), result


class ProxySelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    site_ids: set[Annotated[str, Field(pattern=r"^[a-f0-9]{32}$")]] = Field(max_length=1000)


def tvbox_proxy_routes(path: Path, proxy_url: str | None) -> dict[str, httpx.URL]:
    try:
        content = path.read_bytes()
    except FileNotFoundError:
        return {}
    try:
        selection = ProxySelection.model_validate_json(content)
        if not selection.site_ids:
            return {}
        proxy = httpx.URL(proxy_url or "")
        if (
            proxy.scheme != "http" or not proxy.host or proxy.userinfo or proxy.path not in ("", "/")
            or proxy.query or proxy.fragment
        ):
            raise ValueError("TVBox 需要可用的 HTTP CONNECT 代理")
    except (httpx.InvalidURL, ValueError):
        raise ValueError("TVBox 代理站点配置无效") from None
    return dict.fromkeys(selection.site_ids, proxy)


class TvBoxService:
    def __init__(self, database: Database, *, proxy_routes: dict[str, httpx.URL] | None = None) -> None:
        self.database = database
        self.proxy_routes = proxy_routes or {}
        self.browser_streams = BrowserStreams()
        self._posters: OrderedDict[tuple[str, str], tuple[float, str, str]] = OrderedDict()
        self._poster_images: OrderedDict[tuple[str, str], tuple[float, bytes, str, str]] = OrderedDict()
        self._updates = asyncio.Lock()
        self._requests = asyncio.Semaphore(8)
        self._site_checks = asyncio.Semaphore(4)
        self._image_requests = asyncio.Semaphore(4)
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
            if content:
                parsed = config_json(content)
                if parsed.get("format") in (RELAY_TRANSFER_FORMAT, "shadow-relay-data/v1"):
                    transfer = RelayTransfer.model_validate(parsed)
                    if transfer.format != RELAY_TRANSFER_FORMAT:
                        transfer = prepare_transfer(transfer)
                    result = await self.import_config(None, relay_config(transfer, allow_http), allow_http)
                    result.migrated = await asyncio.to_thread(
                        archive_transfer, self.database, transfer, allow_http
                    )
                    return result
            return await self.import_config(url, content, allow_http, base_url=base_url)
        except TvBoxError as exc:
            return ImportResult(warnings=[str(exc)])
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
                query = query.where(
                    TvBoxSite.enabled, ~TvBoxSite.hidden, TvBoxSite.active_revision.is_not(None)
                )
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
            source = SiteInput(
                name=site.name,
                endpoint=site.endpoint,
                allow_http=site.allow_http,
                protocol_type=site.protocol_type,
            )
            return SiteCatalog(
                source, [Category.model_validate(c) for c in revision.categories],
                self.proxy_routes.get(site_id),
            )

    async def _check(self, source: SiteInput) -> list[Category]:
        proxy = self.proxy_routes.get(identity(source.endpoint))
        async with self._site_checks, self._requests, asyncio.timeout(22):
            payload = await SiteCatalog(source, [], proxy).fetch()
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
            catalog = SiteCatalog(source, categories, proxy)
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
                        await playback_mime(
                            track.url, allow_http=source.allow_http, headers=track.headers, proxy=proxy,
                        )
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
                    protocol_type=source.protocol_type,
                    priority=0,
                )
                session.add(site)
                session.flush()
            site.checked_at = utc_now()
            site.failure = failure
            if (
                categories
                and site.active_revision == baseline
                and (not site.pinned or not site.active_revision)
            ):
                data = [category.model_dump() for category in categories]
                revision = identity(
                    site_id,
                    str(source.protocol_type),
                    str(source.allow_http),
                    json.dumps(data, sort_keys=True),
                )
                if session.get(TvBoxRevision, revision) is None:
                    session.add(
                        TvBoxRevision(
                            id=revision,
                            site_id=site_id,
                            categories=data,
                            protocol_type=source.protocol_type,
                            allow_http=source.allow_http,
                        )
                    )
                if site.active_revision != revision:
                    site.previous_revision = site.active_revision
                    site.active_revision = revision
                site.allow_http = source.allow_http
                site.protocol_type = source.protocol_type
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

    def packs(self) -> list[PackView]:
        with self.database.session() as session:
            return [
                PackView(
                    id=row.id,
                    label=httpx.URL(row.url).host if row.url else "本地导入",
                    site_count=len(row.site_ids),
                    checked_at=row.checked_at,
                    sync_minutes=row.sync_minutes,
                    remote=row.url is not None,
                )
                for row in session.scalars(select(TvBoxPack).order_by(TvBoxPack.checked_at.desc()))
            ]

    def refresh_pack(self, pack_id: str) -> ImportJob:
        with self.database.session() as session:
            row = session.get(TvBoxPack, pack_id)
            if row is None or not row.url:
                raise LookupError("此配置没有可刷新的上游地址")
            return self.submit_import(row.url, None, row.allow_http, None)

    async def schedule(self) -> None:
        while True:
            await asyncio.sleep(60)
            with self.database.session() as session:
                packs = [
                    (pack.url, pack.allow_http)
                    for pack in session.scalars(select(TvBoxPack))
                    if pack.url
                    and pack.sync_minutes > 0
                    and (utc_now() - pack.checked_at.replace(tzinfo=UTC)).total_seconds()
                    >= pack.sync_minutes * 60
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
                    SiteInput(
                        name=site.name,
                        endpoint=site.endpoint,
                        allow_http=site.allow_http,
                        protocol_type=site.protocol_type,
                    )
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
                SiteInput(
                    name=site.name,
                    endpoint=site.endpoint,
                    allow_http=site.allow_http,
                    protocol_type=site.protocol_type,
                )
            )

    def revisions(self, site_id: str) -> list[RevisionView]:
        self._site(site_id)
        with self.database.session() as session:
            query = (
                select(TvBoxRevision)
                .where(TvBoxRevision.site_id == site_id)
                .order_by(
                    TvBoxRevision.created_at.desc(),
                    TvBoxRevision.id,
                )
            )
            return [RevisionView.model_validate(row) for row in session.scalars(query.limit(30))]

    def update(
        self,
        site_id: str,
        *,
        enabled: bool | None,
        name: str | None,
        priority: int | None,
        rollback: bool,
        hidden: bool | None = None,
        pinned: bool | None = None,
        revision_id: str | None = None,
    ) -> SiteStatus:
        with self.database.session() as session:
            site = session.get(TvBoxSite, site_id)
            if site is None:
                raise LookupError("站点不存在")
            desired = site.previous_revision if rollback else revision_id
            if rollback and not desired:
                raise TvBoxError("没有上一可用修订")
            if desired and desired != site.active_revision:
                revision = session.get(TvBoxRevision, desired)
                if revision is None or revision.site_id != site_id:
                    raise LookupError("修订不存在")
                site.previous_revision, site.active_revision = site.active_revision, desired
                if revision.protocol_type is not None:
                    site.protocol_type = revision.protocol_type
                if revision.allow_http is not None:
                    site.allow_http = revision.allow_http
                site.pinned = True
            if hidden is not None:
                site.hidden = hidden
            if pinned is not None:
                site.pinned = pinned
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
            items=[self.card(site_id, row) for row in _records(payload) if current.accepts(row)],
            next_page=page + 1 if page < min(MAX_PAGE, _integer(payload.get("pagecount"))) else None,
        )

    async def search(self, query: str, page: int, *, site_ids: set[str] | None = None) -> Page:
        sites = [site for site in self.sites() if site_ids is None or site.id in site_ids][:MAX_SITES]

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
            **self.card(site_id, row).model_dump(),
            episodes=[track.entry for track in current.tracks(row)],
        ).model_copy(update={"overview": HTMLParser(row.vod_content).text(separator=" ", strip=True)[:4000]})

    async def resolve(self, site_id: str, item_id: str, episode_id: str) -> Playback:
        catalog = self._catalog(site_id)
        async with self._requests:
            row = await catalog.detail(_item_id(item_id))
            tracks = catalog.tracks(row)
            selected = next((track for track in tracks if track.entry.id == episode_id), None)
            if selected is None:
                raise LookupError("分集或线路已变更")
            matching = [track for track in tracks if track.entry.title == selected.entry.title]
            line_counts = Counter(track.entry.line for track in matching)
            backups = [
                track for track in matching
                if track.entry.line and track.entry.line != selected.entry.line
                and line_counts[track.entry.line] == 1
            ] if line_counts[selected.entry.line] == 1 else []
            try:
                async with asyncio.timeout(16):
                    for track in [selected, *backups[:2]]:
                        try:
                            mime = await playback_mime(
                                track.url, allow_http=catalog.site.allow_http, headers=track.headers,
                                proxy=catalog.proxy,
                            )
                        except TvBoxError:
                            continue
                        if not self._catalog(site_id).accepts(row):
                            raise LookupError("分类已变更")
                        return Playback(url=track.url, mime_type=mime, headers=track.headers)
            except TimeoutError as exc:
                raise TvBoxError("分集线路读取超时") from exc
        raise TvBoxError("这集暂时没有可播放的线路")

    async def watch(self, site_id: str, item_id: str, episode_id: str, *, stream_prefix: str) -> Playback:
        stream = await self.resolve(site_id, item_id, episode_id)
        site = self._catalog(site_id).site
        path = self.browser_streams.issue(
            site_id,
            stream.url,
            stream.mime_type,
            stream.headers,
            allow_http=site.allow_http,
            prefix=stream_prefix,
            proxy=self.proxy_routes.get(site_id),
        )
        return Playback(url=path, mime_type=stream.mime_type, headers={})

    def check_stream_site(self, site_id: str) -> None:
        self._catalog(site_id)

    def card(self, site_id: str, row: Vod) -> Card:
        card = _card(site_id, row)
        if row.vod_pic:
            self._posters[(site_id, card.id)] = (monotonic() + 300, row.vod_pic, row.type_id)
            if len(self._posters) > 3000:
                self._posters.popitem(last=False)
        return card

    async def image(self, site_id: str, item_id: str) -> tuple[bytes, str]:
        key = (site_id, item_id)
        catalog = self._catalog(site_id)
        image = self._poster_images.pop(key, None)
        if image and image[0] > monotonic() and image[3] in catalog.allowed:
            self._poster_images[key] = image
            return image[1], image[2]
        async with self._image_requests:
            catalog = self._catalog(site_id)
            cached = self._posters.get(key)
            if cached and cached[0] > monotonic() and cached[2] in catalog.allowed:
                _, poster, category = cached
            else:
                row = await catalog.detail(_item_id(item_id))
                poster = row.vod_pic
                category = row.type_id
            if not poster:
                raise LookupError("没有封面")
            try:
                response = await download(
                    urljoin(catalog.site.endpoint, poster),
                    allow_http=catalog.site.allow_http, proxy=catalog.proxy,
                )
            except TvBoxError as exc:
                logger.warning(
                    "TVBox poster fetch failed site=%s proxy=%s error=%s cause=%s",
                    site_id, catalog.proxy is not None, exc, type(exc.__cause__).__name__,
                )
                raise
            if category not in self._catalog(site_id).allowed:
                raise LookupError("分类已变更")
            if response.mime not in ("image/jpeg", "image/png", "image/webp", "image/gif"):
                raise LookupError("封面格式不受支持")
            self._poster_images[key] = (monotonic() + 300, response.body, response.mime, category)
            cache_bytes = sum(len(entry[1]) for entry in self._poster_images.values())
            while len(self._poster_images) > POSTER_CACHE_ENTRIES or cache_bytes > POSTER_CACHE_BYTES:
                _, removed = self._poster_images.popitem(last=False)
                cache_bytes -= len(removed[1])
            return response.body, response.mime
