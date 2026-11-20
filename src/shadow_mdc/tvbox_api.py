"""TVBox management and mobile-safe playback routes."""

import asyncio
import hashlib
import json
from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

import httpx
from fastapi import APIRouter, Form, HTTPException, Query, Request, Response, UploadFile
from pydantic import BaseModel, Field, ValidationError, model_validator

from .services.tvbox import Category, Detail, ImportJob, PackView, Page, Playback, RevisionView, SiteStatus
from .services.tvbox_http import TvBoxError
from .services.tvbox_publish import BindingInput, BindingView, GroupInput, GroupView, IssuedBinding
from .services.tvbox_relay import RelayRevisionView, historical_config, read_relay_upload, relay_history

if TYPE_CHECKING:
    from .api import Runtime


class ImportRequest(BaseModel):
    url: str | None = Field(default=None, min_length=1, max_length=4096)
    content: str | None = Field(default=None, min_length=1, max_length=2 * 1024 * 1024)
    base_url: str | None = Field(default=None, max_length=4096)
    allow_http: bool = False

    @model_validator(mode="after")
    def check_input(self) -> "ImportRequest":
        if bool(self.url) == bool(self.content):
            raise ValueError("请选择地址或配置文件")
        return self


class SitePatch(BaseModel):
    enabled: bool | None = None
    name: str | None = Field(default=None, min_length=1, max_length=200)
    priority: int | None = Field(default=None, ge=-1000, le=1000)
    rollback: bool = False
    hidden: bool | None = None
    pinned: bool | None = None
    revision_id: str | None = Field(default=None, pattern=r"^[a-f0-9]{32}$")


class Selection(BaseModel):
    episode_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def build_tvbox_router(runtime: Callable[[Request], "Runtime"], *, management: bool) -> APIRouter:
    router = APIRouter(prefix="/api/tvbox" if management else "/tvbox")

    @router.get("/groups")
    def groups(request: Request) -> dict[str, list[GroupView]]:
        return {"items": runtime(request).tvbox_publishing.groups()}

    @router.get("/sites")
    def sites(request: Request) -> dict[str, list[SiteStatus]]:
        return {"items": runtime(request).tvbox.sites(all_sites=management)}

    @router.get("/sites/{site_id}/categories")
    def categories(request: Request, site_id: str) -> dict[str, list[Category]]:
        try:
            return {"items": runtime(request).tvbox.categories(site_id)}
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc

    @router.get("/sites/{site_id}/items")
    async def page(
        request: Request,
        site_id: str,
        category: Annotated[str | None, Query(max_length=100)] = None,
        q: Annotated[str, Query(max_length=200)] = "",
        page: Annotated[int, Query(ge=1, le=500)] = 1,
    ) -> Page:
        try:
            return await runtime(request).tvbox.page(site_id, category, q.strip(), page)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TvBoxError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/search")
    async def search(
        request: Request,
        q: Annotated[str, Query(min_length=1, max_length=200)],
        page: Annotated[int, Query(ge=1, le=500)] = 1,
        group_id: Annotated[str | None, Query(max_length=36)] = None,
    ) -> Page:
        if not q.strip():
            return Page(items=[])
        rt = runtime(request)
        group = next((group for group in rt.tvbox_publishing.groups() if group.id == group_id), None)
        if group_id and group is None:
            raise HTTPException(404, "仓库不存在")
        return await rt.tvbox.search(q.strip(), page, site_ids=set(group.site_ids) if group else None)

    @router.get("/sites/{site_id}/items/{item_id}")
    async def detail(request: Request, site_id: str, item_id: str) -> Detail:
        try:
            return await runtime(request).tvbox.detail(site_id, item_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TvBoxError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.get("/sites/{site_id}/items/{item_id}/image")
    async def image(request: Request, site_id: str, item_id: str) -> Response:
        try:
            data, mime = await runtime(request).tvbox.image(site_id, item_id)
            return Response(data, media_type=mime, headers={"Cache-Control": "private, max-age=300"})
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TvBoxError as exc:
            raise HTTPException(502, str(exc)) from exc

    @router.post("/sites/{site_id}/items/{item_id}/resolve")
    async def resolve(
        request: Request,
        response: Response,
        site_id: str,
        item_id: str,
        selection: Selection,
    ) -> Playback:
        try:
            response.headers["Cache-Control"] = "private, no-store"
            return await runtime(request).tvbox.resolve(site_id, item_id, selection.episode_id)
        except LookupError as exc:
            raise HTTPException(404, str(exc)) from exc
        except TvBoxError as exc:
            raise HTTPException(502, str(exc)) from exc

    if management:

        @router.post("/sites/{site_id}/items/{item_id}/watch")
        async def watch(
            request: Request, response: Response, site_id: str, item_id: str, selection: Selection
        ) -> Playback:
            response.headers["Cache-Control"] = "private, no-store"
            try:
                return await runtime(request).tvbox.watch(site_id, item_id, selection.episode_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(502, str(exc)) from exc

        @router.get("/stream/{key}/{resource}")
        async def stream(request: Request, key: str, resource: str) -> Response:
            service = runtime(request).tvbox
            try:
                session = service.browser_streams.lookup(key)
                service.check_stream_site(session.site_id)
                return await service.browser_streams.response(session, resource, request.headers.get("range"))
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(502, str(exc)) from exc
            except (TimeoutError, OSError, httpx.HTTPError) as exc:
                raise HTTPException(502, "播放资源暂时无法读取") from exc

        @router.post("/groups")
        def create_group(request: Request, data: GroupInput) -> GroupView:
            try:
                return runtime(request).tvbox_publishing.save_group(data)
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

        @router.put("/groups/{group_id}")
        def edit_group(request: Request, group_id: str, data: GroupInput) -> GroupView:
            try:
                return runtime(request).tvbox_publishing.save_group(data, group_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

        @router.get("/subscriptions")
        def bindings(request: Request) -> list[BindingView]:
            return runtime(request).tvbox_publishing.bindings()

        @router.post("/subscriptions")
        def create_binding(request: Request, response: Response, data: BindingInput) -> IssuedBinding:
            response.headers["Cache-Control"] = "no-store"
            try:
                return runtime(request).tvbox_publishing.issue(data)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        @router.post("/subscriptions/{binding_id}/rotate")
        def rotate_binding(request: Request, response: Response, binding_id: str) -> IssuedBinding:
            response.headers["Cache-Control"] = "no-store"
            try:
                return runtime(request).tvbox_publishing.rotate(binding_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

        @router.delete("/subscriptions/{binding_id}")
        def revoke_binding(request: Request, binding_id: str) -> BindingView:
            try:
                return runtime(request).tvbox_publishing.revoke(binding_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        @router.get("/packs")
        def packs(request: Request) -> list[PackView]:
            return runtime(request).tvbox.packs()

        @router.post("/packs/{pack_id}/refresh", status_code=202)
        async def refresh_pack(request: Request, pack_id: str) -> ImportJob:
            try:
                return runtime(request).tvbox.refresh_pack(pack_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

        @router.get("/relay-history")
        def history(request: Request) -> list[RelayRevisionView]:
            return relay_history(runtime(request).database)

        @router.post("/relay-history/{revision_id}/restore", status_code=202)
        async def restore(request: Request, revision_id: str, allow_http: bool = False) -> ImportJob:
            rt = runtime(request)
            try:
                content = historical_config(rt.database, revision_id, allow_http)
                return rt.tvbox.submit_import(None, content, allow_http, None)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

        @router.post("/imports", status_code=202)
        async def import_config(request: Request, payload: ImportRequest) -> ImportJob:
            try:
                return runtime(request).tvbox.submit_import(
                    payload.url,
                    payload.content,
                    payload.allow_http,
                    payload.base_url,
                )
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

        @router.post("/imports/relay", status_code=202)
        async def import_relay_archive(
            request: Request,
            file: UploadFile,
            allow_http: Annotated[bool, Form()] = False,
        ) -> ImportJob:
            if file.size is None or file.size > 64 * 1024 * 1024:
                raise HTTPException(413, "Relay 导出归档应小于 64 MiB")
            try:
                transfer = await asyncio.to_thread(read_relay_upload, file.file, file.filename or "")
                return runtime(request).tvbox.submit_import(
                    None, transfer.model_dump_json(), allow_http, None
                )
            except ValidationError as exc:
                raise HTTPException(422, "Relay 导出清单结构无效") from exc
            except TvBoxError as exc:
                raise HTTPException(422, str(exc)) from exc
            finally:
                await file.close()

        @router.get("/imports/{job_id}")
        async def import_job(request: Request, job_id: str) -> ImportJob:
            try:
                return runtime(request).tvbox.import_job(job_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        @router.post("/sites/{site_id}/refresh")
        async def refresh(request: Request, site_id: str) -> SiteStatus:
            try:
                return await runtime(request).tvbox.refresh(site_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        @router.get("/sites/{site_id}/revisions")
        def revisions(request: Request, site_id: str) -> list[RevisionView]:
            try:
                return runtime(request).tvbox.revisions(site_id)
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc

        @router.patch("/sites/{site_id}")
        async def update(request: Request, site_id: str, patch: SitePatch) -> SiteStatus:
            try:
                return runtime(request).tvbox.update(
                    site_id,
                    enabled=patch.enabled,
                    name=patch.name,
                    priority=patch.priority,
                    rollback=patch.rollback,
                    hidden=patch.hidden,
                    pinned=patch.pinned,
                    revision_id=patch.revision_id,
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

    return router


def build_tvbox_feed_router(runtime: Callable[[Request], "Runtime"]) -> APIRouter:
    router = APIRouter(prefix="/api/tvbox-feed")

    @router.get("/{token}/store.json")
    @router.get("/{token}/sites.json")
    @router.get("/{token}/v/{revision}/{group_id}.json")
    def feed(
        request: Request, token: str, revision: str | None = None, group_id: str | None = None
    ) -> Response:
        try:
            version, groups = runtime(request).tvbox_publishing.publication(token, revision)
        except LookupError as exc:
            raise HTTPException(404, str(exc), headers={"Cache-Control": "private, no-store"}) from exc
        path = request.url.path
        if path.endswith("/store.json") and not revision:
            root = str(request.url.replace(path=path.removesuffix("/store.json"), query="", fragment=""))
            payload: dict[str, object] = {
                "urls": [
                    {"name": group.name, "url": f"{root}/v/{version}/{group.id}.json"} for group in groups
                ],
            }
        else:
            chosen = next((group for group in groups if group.id == group_id), None)
            if group_id and chosen is None:
                raise HTTPException(404, "仓库不存在", headers={"Cache-Control": "private, no-store"})
            sites = {
                site.key: site.model_dump()
                for group in ([chosen] if chosen else groups)
                for site in group.sites
            }
            payload = {"sites": list(sites.values())}
        body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode()
        etag = '"' + hashlib.sha256(body).hexdigest() + '"'
        headers = {
            "Cache-Control": "private, no-cache",
            "ETag": etag,
            "Referrer-Policy": "no-referrer",
            "X-Content-Type-Options": "nosniff",
        }
        if etag in request.headers.get("if-none-match", "").split(","):
            return Response(status_code=304, headers=headers)
        return Response(body, media_type="application/json", headers=headers)

    return router
