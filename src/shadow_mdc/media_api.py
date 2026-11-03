"""Small, authenticated mobile catalog and verified Emby playback contract."""

from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Annotated

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.dialects.sqlite import insert

from .catalog_api import build_catalog_router, catalog_credit_names
from .db.models import MediaEmbyBinding, Work
from .services.playback import (
    PlaybackSettings,
    PlaySource,
    load_sites_from_settings,
    matches_work_code,
    resolve_play,
)

if TYPE_CHECKING:
    from .api import Runtime


class MediaCard(BaseModel):
    id: str
    title: str
    code: str | None
    overview: str | None = None
    actors: list[str]
    duration_ms: int | None
    poster: str | None = None


class MediaPage(BaseModel):
    items: list[MediaCard]
    next_cursor: str | None


class EmbyTarget(BaseModel):
    server_id: str
    item_id: str



class OnlinePlan(BaseModel):
    emby: EmbyTarget | None = None
    sources: list[PlaySource] = Field(default_factory=list)


class BindingChoice(BaseModel):
    item_id: str



class EmbyItem(BaseModel):
    id: str = Field(alias="Id", min_length=1, pattern=r"^[A-Za-z0-9-]{1,100}$")
    name: str = Field(default="", alias="Name")
    type: str = Field(default="", alias="Type")


class EmbyPage(BaseModel):
    items: list[EmbyItem] = Field(default_factory=list, alias="Items")
    total: int = Field(default=0, alias="TotalRecordCount")


class EmbyCandidate(BaseModel):
    id: str
    title: str


def _emby_page(payload: object) -> EmbyPage:
    try:
        return EmbyPage.model_validate(payload)
    except ValidationError as exc:
        raise HTTPException(502, "invalid Emby response") from exc


def build_media_router(runtime: Callable[[Request], Runtime]) -> APIRouter:
    """Build routes without importing the application singleton."""

    def authorize(request: Request, authorization: Annotated[str | None, Header()] = None) -> None:
        token = runtime(request).media_server_store.load().media_client_token
        if not token:
            raise HTTPException(503, "mobile access is not configured")
        if not secrets.compare_digest((authorization or "").encode(), f"Bearer {token}".encode()):
            raise HTTPException(401, "invalid mobile token")

    router = APIRouter(prefix="/api/media/v1", dependencies=[Depends(authorize)])

    def card(work: Work, *, detail: bool = False) -> MediaCard:
        return MediaCard(
            id=work.id, title=work.title, code=work.primary_code,
            overview=work.plot if detail else None, actors=work.actors,
            duration_ms=work.runtime_seconds * 1000 if work.runtime_seconds else None,
            poster=f"/api/media/v1/works/{work.id}/poster",
        )

    @router.get("/capabilities")
    def capabilities() -> dict[str, object]:
        return {"api_version": 1, "features": [
            "browse", "search", "detail", "emby", "binding", "catalog", "playback_plan",
            "people_v2", "playlists", "organizations", "media_mime",
        ]}

    @router.get("/works", response_model=MediaPage)
    def works(
        request: Request, q: Annotated[str, Query(max_length=200)] = "",
        cursor: Annotated[str | None, Query(max_length=2048)] = None,
        limit: Annotated[int, Query(ge=1, le=100)] = 60,
    ) -> MediaPage:
        query = q.strip()[:200]
        statement = select(Work)
        if query:
            statement = statement.where(or_(
                Work.primary_code.icontains(query, autoescape=True),
                Work.title.icontains(query, autoescape=True),
                Work.actors.cast(Work.title.type).icontains(query, autoescape=True),
            ))
        query_hash = hashlib.sha256(query.encode()).hexdigest()
        if cursor:
            try:
                stamp, work_id, scope = json.loads(base64.urlsafe_b64decode(cursor))
                if not isinstance(stamp, str) or not isinstance(work_id, str) or scope != query_hash:
                    raise ValueError("query changed")
                created = datetime.fromisoformat(stamp)
                statement = statement.where(or_(
                    Work.created_at < created,
                    and_(Work.created_at == created, Work.id < work_id),
                ))
            except (ValueError, TypeError, KeyError) as exc:
                raise HTTPException(400, "invalid cursor") from exc
        statement = statement.order_by(Work.created_at.desc(), Work.id.desc()).limit(limit + 1)
        with runtime(request).database.session() as session:
            rows = list(session.scalars(statement))
            page = rows[:limit]
            names = catalog_credit_names(runtime(request), session, page)
            items = [card(work).model_copy(update={"actors": names[work.id]})
                     if work.id in names else card(work) for work in page]
            next_cursor = None
            if len(rows) > limit:
                last = page[-1]
                next_cursor = base64.urlsafe_b64encode(json.dumps(
                    [last.created_at.isoformat(), last.id, query_hash],
                ).encode()).decode()
            return MediaPage(items=items, next_cursor=next_cursor)

    @router.get("/works/{work_id}", response_model=MediaCard)
    def detail(work_id: str, request: Request) -> MediaCard:
        with runtime(request).database.session() as session:
            work = session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            names = catalog_credit_names(runtime(request), session, [work])
            result = card(work, detail=True)
            if work_id in names:
                result.actors = names[work_id]
            return result

    @router.get("/works/{work_id}/poster")
    def poster(work_id: str, request: Request) -> FileResponse:
        # Only generated local artwork is exposed through the mobile contract.
        directory = runtime(request).settings.data_dir / "artwork" / work_id
        if not re.fullmatch(r"[A-Za-z0-9-]+", work_id):
            raise HTTPException(404)
        for stem in ("thumb", "poster", "fanart"):
            for extension in (".jpg", ".jpeg", ".png", ".webp"):
                path = directory / f"{stem}{extension}"
                if path.is_file():
                    return FileResponse(path)
        raise HTTPException(404, "poster not available")

    async def emby_get(request: Request, path: str, params: dict[str, str] | None = None) -> object:
        app = runtime(request)
        settings = app.media_server_store.load()
        if not settings.enabled or not settings.base_url or not settings.api_key:
            raise HTTPException(409, "Emby connection is not configured")
        try:
            response = await app.http.get(
                f"{settings.base_url.rstrip('/')}/{path}",
                headers={"X-Emby-Token": settings.api_key}, params=params, timeout=12,
            )
            if response.status_code == 404:
                raise HTTPException(404, "Emby item no longer exists")
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HTTPException(502, "Emby is temporarily unavailable") from exc

    async def server_id(request: Request) -> str:
        info = await emby_get(request, "System/Info/Public")
        if not isinstance(info, dict) or not isinstance(info.get("Id"), str):
            raise HTTPException(502, "Emby server identity is unavailable")
        return str(info["Id"])

    @router.post("/works/{work_id}/resolve", response_model=EmbyTarget)
    async def resolve(work_id: str, request: Request) -> EmbyTarget:
        instance = await server_id(request)
        with runtime(request).database.session() as session:
            work = session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            binding = session.get(MediaEmbyBinding, work_id)
            if binding is not None and binding.server_id == instance:
                try:
                    await emby_get(request, f"Items/{binding.item_id}")
                    return EmbyTarget(server_id=instance, item_id=binding.item_id)
                except HTTPException as exc:
                    if exc.status_code != 404:
                        raise
            if not work.primary_code:
                raise HTTPException(409, "请选择一次 Emby 条目以建立关联")
            payload = await emby_get(request, "Items", {
                "SearchTerm": work.primary_code, "Recursive": "true",
                "IncludeItemTypes": "Movie,Video,Episode", "Fields": "Path", "Limit": "100",
            })
            page = _emby_page(payload)
            matches = [item for item in page.items
                       if item.type in ("Movie", "Video", "Episode")
                       and matches_work_code(item.name, work.primary_code)]
            if len(matches) != 1 or page.total > 100:
                raise HTTPException(409, "Emby 匹配不唯一, 请选择一次对应条目")
            target = EmbyTarget(server_id=instance, item_id=matches[0].id)
            statement = insert(MediaEmbyBinding).values(work_id=work_id, **target.model_dump())
            session.execute(statement.on_conflict_do_update(
                index_elements=["work_id"], set_=target.model_dump(),
            ))
            return target

    @router.get("/works/{work_id}/emby-candidates")
    async def candidates(
        work_id: str, request: Request, q: Annotated[str, Query(max_length=200)] = "",
    ) -> list[EmbyCandidate]:
        with runtime(request).database.session() as session:
            work = session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            query = q.strip()[:200] or work.primary_code or work.title
        payload = await emby_get(request, "Items", {
            "SearchTerm": query, "Recursive": "true",
            "IncludeItemTypes": "Movie,Video,Episode", "Limit": "50",
        })
        return [EmbyCandidate(id=item.id, title=item.name) for item in _emby_page(payload).items
                if item.type in ("Movie", "Video", "Episode")]

    @router.put("/works/{work_id}/binding", response_model=EmbyTarget)
    async def bind(work_id: str, choice: BindingChoice, request: Request) -> EmbyTarget:
        if not re.fullmatch(r"[A-Za-z0-9-]{1,100}", choice.item_id):
            raise HTTPException(400, "invalid item id")
        instance = await server_id(request)
        item = await emby_get(request, f"Items/{choice.item_id}")
        if not isinstance(item, dict) or item.get("Type") not in ("Movie", "Video", "Episode"):
            raise HTTPException(409, "请选择可播放的 Emby 条目")
        with runtime(request).database.session() as session:
            if session.get(Work, work_id) is None:
                raise HTTPException(404, "work not found")
            target = EmbyTarget(server_id=instance, item_id=choice.item_id)
            statement = insert(MediaEmbyBinding).values(work_id=work_id, **target.model_dump())
            session.execute(statement.on_conflict_do_update(
                index_elements=["work_id"], set_=target.model_dump(),
            ))
        return target

    @router.post("/works/{work_id}/plan", response_model=OnlinePlan)
    async def playback_plan(work_id: str, request: Request, online: bool = False) -> OnlinePlan:
        if not online:
            with runtime(request).database.session() as session:
                has_binding = session.get(MediaEmbyBinding, work_id) is not None
            try:
                return OnlinePlan(emby=await resolve(work_id, request))
            except HTTPException as exc:
                if has_binding or exc.status_code not in (409, 502):
                    raise
        app = runtime(request)
        with app.database.session() as session:
            work = session.get(Work, work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            code, title = work.primary_code, work.title
        media = app.media_server_store.load().model_copy(update={"enabled": False})
        result = await resolve_play(
            work_id=work_id, code=code, title=title, media=media,
            sites=load_sites_from_settings(PlaybackSettings(), data_dir=app.settings.data_dir),
            client=app.http,
        )
        return OnlinePlan(sources=[source for source in result.sources if source.playable_in_app
                                  and source.probe_ok is not False])

    router.include_router(build_catalog_router(runtime, prefix="/catalog"))
    return router
