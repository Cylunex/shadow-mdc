"""TVBox management and mobile-safe playback routes."""

from collections.abc import Callable
from typing import TYPE_CHECKING, Annotated

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field, model_validator

from .services.tvbox import Category, Detail, ImportJob, Page, Playback, SiteStatus
from .services.tvbox_http import TvBoxError

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


class Selection(BaseModel):
    episode_id: str = Field(pattern=r"^[a-f0-9]{32}$")


def build_tvbox_router(runtime: Callable[[Request], "Runtime"], *, management: bool) -> APIRouter:
    router = APIRouter(prefix="/api/tvbox" if management else "/tvbox")

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
    ) -> Page:
        if not q.strip():
            return Page(items=[])
        return await runtime(request).tvbox.search(q.strip(), page)

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

        @router.patch("/sites/{site_id}")
        async def update(request: Request, site_id: str, patch: SitePatch) -> SiteStatus:
            try:
                return runtime(request).tvbox.update(
                    site_id,
                    enabled=patch.enabled,
                    name=patch.name,
                    priority=patch.priority,
                    rollback=patch.rollback,
                )
            except LookupError as exc:
                raise HTTPException(404, str(exc)) from exc
            except TvBoxError as exc:
                raise HTTPException(409, str(exc)) from exc

    return router
