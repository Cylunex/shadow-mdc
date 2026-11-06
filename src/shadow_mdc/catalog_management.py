"""Organization dossiers and owner-scoped, ordered manual playlists."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import Select, delete, func, select, union, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Subquery

from .collection_names import normalize_collection_name
from .db.models import (
    CatalogOrganization,
    CatalogPlaylist,
    CatalogPlaylistRule,
    CatalogWorkOrganization,
    Collection,
    Work,
    WorkCollection,
)

if TYPE_CHECKING:
    from .api import Runtime

Role = Literal["studio", "distributor", "label", "platform", "series", "credited"]


class OrganizationEdit(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    description: str | None = Field(default=None, max_length=30000)
    parent_id: str | None = None
    revision: int = Field(default=0, ge=0)


    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned



class OrganizationCreate(OrganizationEdit):
    kind: Literal["organization", "studio", "label", "platform", "series"] = "organization"


class OrganizationOut(OrganizationEdit):
    id: str
    kind: str
    work_count: int = 0


class OrganizationLink(BaseModel):
    work_id: str
    role: Role


class CatalogFilter(BaseModel):
    q: str = Field(default="", max_length=200)
    actors: list[str] = Field(default_factory=list)
    actors_all: bool = False
    actor_name: str = ""
    studio_name: str = ""
    tags: list[str] = Field(default_factory=list)
    tags_all: bool = False
    genres: list[str] = Field(default_factory=list)
    forms: list[str] = Field(default_factory=list)
    regions: list[str] = Field(default_factory=list)
    exclude_tags: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    organization_role: str = ""
    year_from: int | None = None
    year_to: int | None = None
    unknown_year: bool = False
    status: Literal["all", "files", "emby", "metadata"] = "all"
    sort: Literal["added", "newest", "oldest", "title"] = "added"
    cursor: str | None = None
    limit: int = Field(default=60, ge=1, le=100)


class PlaylistEdit(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    work_ids: list[str] = Field(default_factory=list, max_length=10000)
    filters: CatalogFilter | None = None
    revision: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned



class PlaylistOut(PlaylistEdit):
    id: str
    work_ids: list[str] = Field(default_factory=list)


class PlaylistMember(BaseModel):
    id: str
    title: str



def organization_relationships() -> Subquery:
    return union(
        select(WorkCollection.collection_id.label("organization_id"), WorkCollection.work_id,
               Collection.kind.label("role")).join(Collection, Collection.id == WorkCollection.collection_id),
        select(CatalogWorkOrganization.organization_id, CatalogWorkOrganization.work_id,
               CatalogWorkOrganization.role),
    ).subquery()


def build_management_router(
    runtime: Callable[[Request], Runtime],
    ordered_works: Callable[[CatalogFilter, Request, Session], Select[tuple[Work]]],
    *, prefix: str, writable: bool,
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def owner(x_catalog_owner: str | None) -> str:
        # A random client namespace scopes device preferences; it is not an authentication token.
        if not x_catalog_owner or re.fullmatch(r"[A-Za-z0-9_-]{1,100}", x_catalog_owner) is None:
            raise HTTPException(400, "a client preference namespace is required")
        return x_catalog_owner

    @router.get("/organizations")
    def organizations(request: Request) -> list[OrganizationOut]:
        with runtime(request).database.session() as session:
            extras = {row.id: row for row in session.scalars(select(CatalogOrganization))}
            links = organization_relationships()
            counts: dict[str, int] = {key: count for key, count in session.execute(select(
                links.c.organization_id, func.count(func.distinct(links.c.work_id)),
            ).group_by(links.c.organization_id))}
            return [OrganizationOut(
                id=row.id, kind=row.kind, name=row.name,
                aliases=extras[row.id].aliases if row.id in extras else row.aliases,
                description=extras[row.id].description if row.id in extras else row.description,
                parent_id=extras[row.id].parent_id if row.id in extras else None,
                revision=extras[row.id].revision if row.id in extras else 0,
                work_count=counts.get(row.id, 0),
            ) for row in session.scalars(select(Collection).order_by(Collection.name, Collection.id))]

    if writable:
        @router.post("/organizations", status_code=201)
        def create_organization(payload: OrganizationCreate, request: Request) -> OrganizationOut:
            with runtime(request).database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                normalized = normalize_collection_name(payload.name)
                if session.scalar(select(Collection.id).where(
                    Collection.kind == payload.kind, Collection.normalized_name == normalized,
                )):
                    raise HTTPException(409, "organization with this name already exists")
                if payload.parent_id and session.get(Collection, payload.parent_id) is None:
                    raise HTTPException(404, "parent organization not found")
                row = Collection(name=payload.name, normalized_name=normalized, kind=payload.kind,
                                 aliases=payload.aliases, description=payload.description)
                session.add(row)
                session.flush()
                session.add(CatalogOrganization(
                    id=row.id, aliases=payload.aliases, description=payload.description,
                    parent_id=payload.parent_id, revision=1,
                ))
                return OrganizationOut(
                    id=row.id, kind=payload.kind, name=payload.name, aliases=payload.aliases,
                    description=payload.description, parent_id=payload.parent_id, revision=1,
                )

        @router.put("/organizations/{organization_id}")
        def edit_organization(
            organization_id: str, payload: OrganizationEdit, request: Request,
        ) -> OrganizationOut:
            with runtime(request).database.session() as session:
                # Serialize hierarchy validation with its update on the supported SQLite store.
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                collection = session.get(Collection, organization_id)
                if collection is None:
                    raise HTTPException(404, "organization not found")
                current = session.get(CatalogOrganization, organization_id)
                if (current.revision if current else 0) != payload.revision:
                    raise HTTPException(409, "organization changed; reload")
                if payload.parent_id:
                    parent: str | None = payload.parent_id
                    visited = {organization_id}
                    while parent:
                        if parent in visited:
                            raise HTTPException(409, "organization hierarchy cannot contain cycles")
                        visited.add(parent)
                        if session.get(Collection, parent) is None:
                            raise HTTPException(404, "parent organization not found")
                        ancestor = session.get(CatalogOrganization, parent)
                        parent = ancestor.parent_id if ancestor else None
                normalized = normalize_collection_name(payload.name)
                duplicate = session.scalar(select(Collection.id).where(
                    Collection.id != organization_id, Collection.kind == collection.kind,
                    Collection.normalized_name == normalized,
                ))
                if duplicate:
                    raise HTTPException(409, "organization with this name already exists")
                aliases = list(dict.fromkeys([
                    *payload.aliases, *([collection.name] if collection.name != payload.name else []),
                ]))
                collection.name = payload.name
                collection.normalized_name = normalized
                collection.description = payload.description
                collection.aliases = aliases
                values = payload.model_dump(exclude={"name"})
                values["revision"] = payload.revision + 1
                values["aliases"] = aliases
                statement = insert(CatalogOrganization).values(id=organization_id, **values)
                changed = session.execute(statement.on_conflict_do_update(
                    index_elements=["id"], set_=values,
                    where=CatalogOrganization.revision == payload.revision,
                ))
                if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
                    raise HTTPException(409, "organization changed; reload")
                relationships = organization_relationships()
                work_count = session.scalar(select(func.count(func.distinct(
                    relationships.c.work_id,
                ))).where(relationships.c.organization_id == organization_id)) or 0
                return OrganizationOut(id=organization_id, kind=collection.kind, work_count=work_count, **{
                    **payload.model_dump(), "revision": payload.revision + 1, "aliases": aliases,
                })

        @router.put("/organizations/{organization_id}/works")
        def link_work(
            organization_id: str, payload: OrganizationLink, request: Request,
        ) -> dict[str, str]:
            with runtime(request).database.session() as session:
                if (session.get(Collection, organization_id) is None
                        or session.get(Work, payload.work_id) is None):
                    raise HTTPException(404, "organization or work not found")
                statement = insert(CatalogWorkOrganization).values(
                    organization_id=organization_id, work_id=payload.work_id, role=payload.role,
                )
                session.execute(statement.on_conflict_do_nothing())
            return {"work_id": payload.work_id, "role": payload.role}

    def playlist_members(
        row: CatalogPlaylist, rule: CatalogPlaylistRule | None, request: Request, session: Session,
    ) -> list[PlaylistMember]:
        if rule is not None:
            statement = ordered_works(CatalogFilter.model_validate(rule.filters), request, session)
            return [PlaylistMember(id=key, title=title) for key, title in
                    session.execute(statement.with_only_columns(Work.id, Work.title))]
        manual = select(Work.id, Work.title).where(Work.id.in_(row.work_ids))
        titles = {key: title for key, title in session.execute(manual)}
        return [PlaylistMember(id=key, title=titles[key]) for key in row.work_ids if key in titles]

    def playlist_out(
        row: CatalogPlaylist, rule: CatalogPlaylistRule | None, request: Request, session: Session,
    ) -> PlaylistOut:
        work_ids = (
            [work.id for work in playlist_members(row, rule, request, session)] if rule else row.work_ids
        )
        return PlaylistOut(
            id=row.id, name=row.name, revision=row.revision, work_ids=work_ids,
            filters=CatalogFilter.model_validate(rule.filters) if rule else None,
        )

    @router.get("/playlists")
    def playlists(
        request: Request, x_catalog_owner: str | None = Header(default=None),
    ) -> list[PlaylistOut]:
        namespace = owner(x_catalog_owner)
        with runtime(request).database.session() as session:
            statement = select(CatalogPlaylist, CatalogPlaylistRule).outerjoin(
                CatalogPlaylistRule, CatalogPlaylistRule.playlist_id == CatalogPlaylist.id,
            ).where(CatalogPlaylist.owner == namespace).order_by(
                CatalogPlaylist.created_at, CatalogPlaylist.id,
            )
            return [
                playlist_out(row, rule, request, session) for row, rule in session.execute(statement).all()
            ]

    @router.get("/playlists/{playlist_id}/works")
    def playlist_works(
        playlist_id: str, request: Request, x_catalog_owner: str | None = Header(default=None),
    ) -> list[PlaylistMember]:
        namespace = owner(x_catalog_owner)
        with runtime(request).database.session() as session:
            row = session.get(CatalogPlaylist, playlist_id)
            if row is None or row.owner != namespace:
                raise HTTPException(404, "playlist not found")
            return playlist_members(row, session.get(CatalogPlaylistRule, playlist_id), request, session)

    @router.post("/playlists", status_code=201)
    def create_playlist(
        payload: PlaylistEdit, request: Request,
        x_catalog_owner: str | None = Header(default=None),
    ) -> PlaylistOut:
        namespace = owner(x_catalog_owner)
        with runtime(request).database.session() as session:
            ids = [] if payload.filters is not None else list(dict.fromkeys(payload.work_ids))
            known = set(session.scalars(select(Work.id).where(Work.id.in_(ids))))
            if set(ids) != known:
                raise HTTPException(404, "one or more works no longer exist")
            row = CatalogPlaylist(name=payload.name, owner=namespace, work_ids=ids, revision=1)
            session.add(row)
            session.flush()
            rule = None
            if payload.filters is not None:
                rule = CatalogPlaylistRule(
                    playlist_id=row.id, filters=payload.filters.model_dump(exclude={"cursor", "limit"}),
                )
                session.add(rule)
            return playlist_out(row, rule, request, session)

    @router.put("/playlists/{playlist_id}")
    def edit_playlist(
        playlist_id: str, payload: PlaylistEdit, request: Request,
        x_catalog_owner: str | None = Header(default=None),
    ) -> PlaylistOut:
        namespace = owner(x_catalog_owner)
        with runtime(request).database.session() as session:
            row = session.get(CatalogPlaylist, playlist_id)
            if row is None or row.owner != namespace:
                raise HTTPException(404, "playlist not found")
            if row.revision != payload.revision:
                raise HTTPException(409, "playlist changed; reload before saving")
            ids = [] if payload.filters is not None else list(dict.fromkeys(payload.work_ids))
            known = set(session.scalars(select(Work.id).where(Work.id.in_(ids))))
            if set(ids) != known:
                raise HTTPException(404, "one or more works no longer exist")
            changed = session.execute(update(CatalogPlaylist).where(
                CatalogPlaylist.id == playlist_id, CatalogPlaylist.owner == namespace,
                CatalogPlaylist.revision == payload.revision,
            ).values(name=payload.name, work_ids=ids, revision=payload.revision + 1))
            if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
                raise HTTPException(409, "playlist changed; reload before saving")
            rule = session.get(CatalogPlaylistRule, playlist_id)
            if payload.filters is not None:
                rule = session.merge(CatalogPlaylistRule(
                    playlist_id=playlist_id, filters=payload.filters.model_dump(exclude={"cursor", "limit"}),
                ))
            elif rule is not None:
                session.delete(rule)
                rule = None
            session.refresh(row)
            return playlist_out(row, rule, request, session)

    @router.delete("/playlists/{playlist_id}", status_code=204)
    def remove_playlist(
        playlist_id: str, request: Request, x_catalog_owner: str | None = Header(default=None),
    ) -> None:
        namespace = owner(x_catalog_owner)
        with runtime(request).database.session() as session:
            owned = select(CatalogPlaylist.id).where(
                CatalogPlaylist.id == playlist_id, CatalogPlaylist.owner == namespace,
            )
            session.execute(delete(CatalogPlaylistRule).where(CatalogPlaylistRule.playlist_id.in_(owned)))
            session.execute(delete(CatalogPlaylist).where(CatalogPlaylist.id.in_(owned)))

    return router
