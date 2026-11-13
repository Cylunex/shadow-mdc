"""Organization dossiers and owner-scoped, ordered manual playlists."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, Header, HTTPException, Request
from pydantic import BaseModel, Field, field_validator, model_validator
from sqlalchemy import Select, delete, func, select, union, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Subquery

from .catalog_gaps import MetadataGap
from .catalog_series import ORGANIZATION_KINDS, ensure_series, find_series, link_series, series_relationships
from .classifications import ClassificationSelection
from .collection_names import normalize_collection_name
from .db.models import (
    CatalogOrganization,
    CatalogPlaylist,
    CatalogPlaylistRule,
    CatalogSeries,
    CatalogWorkOrganization,
    Collection,
    Work,
    WorkCollection,
)
from .enums import MediaAvailability, MediaCategory, OrganizationKind, OrganizationRole

if TYPE_CHECKING:
    from .api import Runtime

Role = OrganizationRole


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
    kind: OrganizationKind = OrganizationKind.ORGANIZATION


class OrganizationOut(OrganizationEdit):
    id: str
    kind: OrganizationKind
    work_count: int = 0


class OrganizationLink(BaseModel):
    work_id: str
    role: Role


class SeriesEdit(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    description: str | None = Field(default=None, max_length=30000)
    organization_id: str | None = None
    revision: int = Field(default=0, ge=0)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()


class SeriesOut(SeriesEdit):
    id: str
    work_count: int = 0


class SeriesMember(BaseModel):
    work_id: str


class CatalogFilter(ClassificationSelection):
    # Read compatibility only: genres now match the unified tag store; subtitles
    # and manual duration forms keep their historical assignments addressable.
    genres: list[str] = Field(default_factory=list, max_length=100)
    subtitle_languages: list[str] = Field(default_factory=list, max_length=100)
    duration_min: int | None = Field(default=None, ge=0, description="最小时长(秒, 含边界)")
    duration_max: int | None = Field(default=None, gt=0, description="最大时长(秒, 不含边界)")
    unknown_duration: bool = False
    missing_metadata: MetadataGap | None = None
    q: str = Field(default="", max_length=200)
    category: MediaCategory | Literal["all"] = "all"
    collection: str = ""
    seed: int = Field(default=0, ge=0, le=1000000)
    actors: list[str] = Field(default_factory=list)
    actors_all: bool = False
    actor_name: str = ""
    studio_name: str = ""
    series_name: str = ""
    series: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    tags_all: bool = False
    exclude_tags: list[str] = Field(default_factory=list)
    organizations: list[str] = Field(default_factory=list)
    labels: list[str] = Field(default_factory=list)
    platforms: list[str] = Field(default_factory=list)
    organization_role: OrganizationRole | Literal["", "series"] = ""
    year_from: int | None = None
    year_to: int | None = None
    unknown_year: bool = False
    status: Literal["all", "files", "emby", "metadata", "want", "downloaded", "undownloaded"] = "all"
    availability: MediaAvailability = MediaAvailability.ALL
    wanted: bool | None = None
    sort: Literal["added", "newest", "oldest", "title", "code", "ranked", "random"] = "added"
    cursor: str | None = None
    limit: int = Field(default=60, ge=1, le=100)

    @property
    def media_availability(self) -> MediaAvailability:
        """The old status is an input preset, not a stored work lifecycle."""
        if self.availability is not MediaAvailability.ALL:
            return self.availability
        return {
            "files": MediaAvailability.FILES, "downloaded": MediaAvailability.FILES,
            "emby": MediaAvailability.EMBY, "metadata": MediaAvailability.METADATA,
            "undownloaded": MediaAvailability.UNDOWNLOADED,
        }.get(self.status, MediaAvailability.ALL)

    @property
    def wanted_filter(self) -> bool | None:
        return self.wanted if self.wanted is not None else True if self.status == "want" else None

    @model_validator(mode="after")
    def validate_duration(self) -> CatalogFilter:
        if (self.duration_min is not None and self.duration_max is not None
                and self.duration_min >= self.duration_max):
            raise ValueError("maximum duration must exceed minimum duration")
        if self.unknown_duration and (self.duration_min is not None or self.duration_max is not None):
            raise ValueError("unknown duration cannot be combined with a duration range")
        return self


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
    """Organization credits, including subsidiaries; series membership is a separate relation."""
    direct = union(
        select(WorkCollection.collection_id.label("organization_id"), WorkCollection.work_id,
               Collection.kind.label("role")).join(Collection, Collection.id == WorkCollection.collection_id)
        .where(Collection.kind.in_(ORGANIZATION_KINDS)),
        select(CatalogWorkOrganization.organization_id, CatalogWorkOrganization.work_id,
               CatalogWorkOrganization.role).join(
                   Collection, Collection.id == CatalogWorkOrganization.organization_id,
               )
        .where(Collection.kind.in_(ORGANIZATION_KINDS), CatalogWorkOrganization.role != "series"),
    ).cte()
    ancestors = select(direct).cte(recursive=True)
    ancestors = ancestors.union(select(
        CatalogOrganization.parent_id, ancestors.c.work_id, ancestors.c.role,
    ).join(CatalogOrganization, CatalogOrganization.id == ancestors.c.organization_id)
        .join(Collection, Collection.id == CatalogOrganization.parent_id)
        .where(Collection.kind.in_(ORGANIZATION_KINDS)))
    return select(ancestors).subquery()


def build_management_router(
    runtime: Callable[[Request], Runtime],
    ordered_works: Callable[[CatalogFilter, Request, Session], Select[tuple[Work]]],
    *, prefix: str, writable: bool,
) -> APIRouter:
    router = APIRouter(prefix=prefix)

    def require_organization(session: Session, identity: str) -> Collection:
        row = session.get(Collection, identity)
        if row is None or row.kind not in ORGANIZATION_KINDS:
            raise HTTPException(404, "organization not found")
        return row

    def require_parent(session: Session, identity: str) -> None:
        if require_organization(session, identity).kind not in ("organization", "studio"):
            raise HTTPException(422, "a parent organization must be a company or studio")

    def series_out(row: CatalogSeries, session: Session, count: int | None = None) -> SeriesOut:
        if count is None:
            links = series_relationships()
            count = session.scalar(select(func.count()).select_from(links).where(
                links.c.series_id == row.id,
            )) or 0
        return SeriesOut(id=row.id, name=row.name, aliases=row.aliases, description=row.description,
                         organization_id=row.organization_id, revision=row.revision, work_count=count)

    @router.get("/series")
    def series(request: Request) -> list[SeriesOut]:
        with runtime(request).database.session() as session:
            links = series_relationships()
            counts: dict[str, int] = {key: count for key, count in session.execute(select(
                links.c.series_id, func.count(),
            ).group_by(
                links.c.series_id,
            ))}
            return [series_out(row, session, counts.get(row.id, 0)) for row in session.scalars(
                select(CatalogSeries).order_by(CatalogSeries.name, CatalogSeries.id),
            )]

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
                id=row.id, kind=OrganizationKind(row.kind), name=row.name,
                aliases=extras[row.id].aliases if row.id in extras else row.aliases,
                description=extras[row.id].description if row.id in extras else row.description,
                parent_id=extras[row.id].parent_id if row.id in extras else None,
                revision=extras[row.id].revision if row.id in extras else 0,
                work_count=counts.get(row.id, 0),
            ) for row in session.scalars(select(Collection).where(
                Collection.kind.in_(ORGANIZATION_KINDS),
            ).order_by(Collection.name, Collection.id))]

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
                if payload.parent_id:
                    require_parent(session, payload.parent_id)
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
                collection = require_organization(session, organization_id)
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
                        require_parent(session, parent)
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
                return OrganizationOut(id=organization_id, kind=OrganizationKind(collection.kind),
                                       work_count=work_count, **{
                    **payload.model_dump(), "revision": payload.revision + 1, "aliases": aliases,
                })

        @router.put("/organizations/{organization_id}/works")
        def link_work(
            organization_id: str, payload: OrganizationLink, request: Request,
        ) -> dict[str, str]:
            with runtime(request).database.session() as session:
                require_organization(session, organization_id)
                if session.get(Work, payload.work_id) is None:
                    raise HTTPException(404, "work not found")
                statement = insert(CatalogWorkOrganization).values(
                    organization_id=organization_id, work_id=payload.work_id, role=payload.role,
                )
                session.execute(statement.on_conflict_do_nothing())
            return {"work_id": payload.work_id, "role": payload.role}

        @router.post("/series", status_code=201)
        def create_series(payload: SeriesEdit, request: Request) -> SeriesOut:
            with runtime(request).database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                if payload.organization_id:
                    require_organization(session, payload.organization_id)
                if find_series(session, payload.name) is not None:
                    raise HTTPException(409, "series with this name already exists")
                row = ensure_series(session, payload.name)
                row.aliases, row.description = payload.aliases, payload.description
                row.organization_id, row.revision = payload.organization_id, 1
                session.flush()
                return series_out(row, session)

        @router.put("/series/{series_id}")
        def edit_series(series_id: str, payload: SeriesEdit, request: Request) -> SeriesOut:
            with runtime(request).database.session() as session:
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                row = session.get(CatalogSeries, series_id)
                if row is None:
                    raise HTTPException(404, "series not found")
                if row.revision != payload.revision:
                    raise HTTPException(409, "series changed; reload")
                if payload.organization_id:
                    require_organization(session, payload.organization_id)
                normalized = normalize_collection_name(payload.name)
                if session.scalar(select(Collection.id).where(
                    Collection.kind == "series", Collection.normalized_name == normalized,
                    Collection.id != series_id,
                )):
                    raise HTTPException(409, "series with this name already exists")
                row.aliases = list(dict.fromkeys([
                    *payload.aliases, *([row.name] if row.name != payload.name else []),
                ]))
                row.name, row.normalized_name = payload.name, normalized
                row.description, row.organization_id = payload.description, payload.organization_id
                row.revision += 1
                legacy = session.get(Collection, series_id)
                if legacy is not None:
                    legacy.name, legacy.normalized_name = row.name, row.normalized_name
                    legacy.aliases, legacy.description = row.aliases, row.description
                session.flush()
                return series_out(row, session)

        @router.put("/series/{series_id}/works")
        def add_series_member(series_id: str, payload: SeriesMember, request: Request) -> dict[str, str]:
            with runtime(request).database.session() as session:
                if (session.get(CatalogSeries, series_id) is None
                        or session.get(Work, payload.work_id) is None):
                    raise HTTPException(404, "series or work not found")
                link_series(session, payload.work_id, series_id)
            return {"work_id": payload.work_id, "series_id": series_id}

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
