"""Unified catalog projection shared by the Web and authenticated mobile views."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from typing import TYPE_CHECKING, Literal
from uuid import NAMESPACE_URL, uuid5

from fastapi import APIRouter, HTTPException, Request
from fastapi.params import Depends
from pydantic import BaseModel, Field
from sqlalchemy import (
    Integer,
    Select,
    SQLColumnExpression,
    String,
    and_,
    case,
    cast,
    exists,
    func,
    or_,
    select,
    true,
)
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from .db.models import (
    Actor,
    Collection,
    MediaAsset,
    MediaEmbyBinding,
    Work,
    WorkActor,
    WorkCollection,
    WorkTaxonomy,
)
from .tags import normalize_tags

if TYPE_CHECKING:
    from .api import Runtime


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



class TaxonomyEdit(BaseModel):
    genres: list[str] = Field(default_factory=list, max_length=100)
    forms: list[str] = Field(default_factory=list, max_length=30)
    regions: list[str] = Field(default_factory=list, max_length=30)
    release_year: int | None = Field(default=None, ge=1800, le=2200)


class CatalogCard(BaseModel):
    id: str
    title: str
    code: str | None
    year: int | None
    actors: list[str]
    tags: list[str]
    studio: str | None
    label: str | None
    series: str | None
    overview: str | None = None
    duration_ms: int | None
    poster: str


class CatalogPage(BaseModel):
    items: list[CatalogCard]
    next_cursor: str | None
    total: int


class CatalogPerson(BaseModel):
    id: str
    ref: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    biography: str | None = None
    image_url: str | None = None
    work_ids: list[str] = Field(default_factory=list)


class Facet(BaseModel):
    id: str
    name: str
    count: int
    role: str = ""


def _card(work: Work, prefix: str, *, detail: bool = False) -> CatalogCard:
    return CatalogCard(
        id=work.id, title=work.title, code=work.primary_code,
        year=work.release_date.year if work.release_date else None,
        actors=work.actors, tags=list(normalize_tags(work.tags)), studio=work.studio,
        label=work.label, series=work.series, overview=work.plot if detail else None,
        duration_ms=work.runtime_seconds * 1000 if work.runtime_seconds else None,
        poster=(f"/api/works/{work.id}/artwork/poster" if prefix == "/api"
                    else f"{prefix}/works/{work.id}/poster"),
    )


def build_catalog_router(
    runtime: Callable[[Request], Runtime], *, prefix: str = "/api/catalog",
    dependencies: list[Depends] | None = None,
) -> APIRouter:
    """Use a compatibility projection so legacy identities and preferences remain addressable."""
    router = APIRouter(prefix=prefix, dependencies=dependencies)

    def people(request: Request, session: Session) -> list[CatalogPerson]:
        relations: dict[str, list[str]] = {}
        for actor_id, work_id in session.execute(select(WorkActor.actor_id, WorkActor.work_id)):
            relations.setdefault(actor_id, []).append(work_id)
        result = {
            actor.id: CatalogPerson(id=actor.id, ref=f"jav:{actor.id}", name=actor.name,
                                    aliases=actor.aliases, image_url=actor.image_url,
                                    work_ids=relations.get(actor.id, []))
            for actor in session.scalars(select(Actor))
        }
        for profile in runtime(request).actor_store.load():
            key = profile.id or str(uuid5(NAMESPACE_URL, f"mdc:actor:{profile.name}"))
            if key in result:
                result[key].work_ids = sorted(set(result[key].work_ids) | {w.id for w in profile.works})
                continue
            result[key] = CatalogPerson(
                id=key, ref=f"jav:{profile.id or profile.name}", name=profile.name,
                aliases=list(profile.aliases), image_url=profile.image_url,
                work_ids=[w.id for w in profile.works],
            )
        # Keep curated names in their original namespace, including zero-work entries.
        # Reuse the existing matching rules rather than merging names across catalogs.
        name_index: dict[str, set[str]] = {}
        for work_id, names in session.execute(select(Work.id, Work.actors)):
            for name in names:
                normalized = unicodedata.normalize("NFKC", name).casefold().strip()
                name_index.setdefault(normalized, set()).add(work_id)
        for curated in runtime(request).non_jav_actor_store.load().actors:
            key = str(uuid5(NAMESPACE_URL, f"mdc:curated:{curated.name}"))
            result[key] = CatalogPerson(
                id=key, ref=f"nj:{curated.name}", name=curated.name,
                aliases=list(curated.aliases), biography=curated.biography,
                image_url=f"/api/non-jav-actor-images/{curated.image_file}" if curated.image_file else None,
                work_ids=sorted(set().union(*(name_index.get(
                    unicodedata.normalize("NFKC", name).casefold().strip(), set(),
                ) for name in [curated.name, *curated.aliases, *curated.match_names]))),
            )
        return list(result.values())

    def filtered_statement(
        filters: CatalogFilter, request: Request, session: Session,
    ) -> Select[tuple[Work]]:
        statement = select(Work)
        if filters.q.strip():
            q = filters.q.strip()
            statement = statement.where(or_(
                Work.title.icontains(q, autoescape=True), Work.primary_code.icontains(q, autoescape=True),
                cast(Work.actors, String).icontains(q, autoescape=True),
                Work.studio.icontains(q, autoescape=True), Work.label.icontains(q, autoescape=True),
                Work.series.icontains(q, autoescape=True),
                Work.id.in_(select(WorkActor.work_id).join(
                    Actor, Actor.id == WorkActor.actor_id,
                ).where(or_(Actor.name.icontains(q, autoescape=True),
                            cast(Actor.aliases, String).icontains(q, autoescape=True)))),
            ))
        if filters.actor_name:
            statement = statement.where(cast(Work.actors, String).icontains(
                filters.actor_name, autoescape=True,
            ))
        if filters.studio_name:
            statement = statement.where(Work.studio == filters.studio_name)
        if filters.actors:
            profiles = {p.id: p for p in people(request, session)}
            groups = [set(profiles[key].work_ids) if key in profiles else set() for key in filters.actors]
            ids = set.intersection(*groups) if filters.actors_all else set.union(*groups)
            statement = statement.where(Work.id.in_(ids))
        if filters.tags or filters.exclude_tags:
            # SQLite json_each avoids materializing every work in Python.
            def tagged(tag: str) -> ColumnElement[bool]:
                values = func.json_each(Work.tags).table_valued("value")
                return exists(select(1).select_from(values).where(values.c.value == tag))
            predicates = [tagged(tag) for tag in filters.tags]
            if predicates:
                statement = (statement.where(*predicates) if filters.tags_all
                             else statement.where(or_(*predicates)))
            for tag in filters.exclude_tags:
                statement = statement.where(~tagged(tag))
        for field, selected in (("genres", filters.genres), ("forms", filters.forms),
                                ("regions", filters.regions)):
            if selected:
                column = {"genres": WorkTaxonomy.genres, "forms": WorkTaxonomy.forms,
                          "regions": WorkTaxonomy.regions}[field]
                values = func.json_each(column).table_valued("value")
                matching = select(WorkTaxonomy.work_id).select_from(WorkTaxonomy).join(
                    values, true(),
                ).where(values.c.value.in_(selected))
                statement = statement.where(Work.id.in_(matching))
        if filters.organizations:
            matching = select(WorkCollection.work_id).join(Collection).where(
                Collection.id.in_(filters.organizations),
            )
            if filters.organization_role:
                matching = matching.where(Collection.kind == filters.organization_role)
            statement = statement.where(Work.id.in_(matching))
        year = func.coalesce(
            select(WorkTaxonomy.release_year).where(WorkTaxonomy.work_id == Work.id).scalar_subquery(),
            cast(func.strftime("%Y", Work.release_date), Integer),
        )
        if filters.unknown_year:
            statement = statement.where(year.is_(None))
        if filters.year_from is not None:
            statement = statement.where(year >= filters.year_from)
        if filters.year_to is not None:
            statement = statement.where(year <= filters.year_to)
        files = exists(select(1).where(MediaAsset.work_id == Work.id))
        emby = exists(select(1).where(MediaEmbyBinding.work_id == Work.id))
        if filters.status == "files":
            statement = statement.where(files)
        elif filters.status == "emby":
            statement = statement.where(emby)
        elif filters.status == "metadata":
            statement = statement.where(~files, ~emby)
        return statement

    def query_works(filters: CatalogFilter, request: Request, session: Session) -> CatalogPage:
        statement = filtered_statement(filters, request, session)
        total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        order_column: SQLColumnExpression[str] | SQLColumnExpression[datetime]
        if filters.sort == "title":
            order_column = Work.title
            ascending = True
        elif filters.sort in ("newest", "oldest"):
            ascending = filters.sort == "oldest"
            explicit_year = select(WorkTaxonomy.release_year).where(
                WorkTaxonomy.work_id == Work.id,
            ).scalar_subquery()
            order_column = func.coalesce(
                case((explicit_year.is_not(None), func.printf("%04d-01-01", explicit_year))),
                cast(Work.release_date, String),
                "9999-12-31" if ascending else "0000-01-01",
            )
        else:
            order_column = Work.created_at
            ascending = False
        scope = hashlib.sha256(filters.model_dump_json(exclude={"cursor", "limit"}).encode()).hexdigest()
        if filters.cursor:
            try:
                cursor_scope, value, last_id = json.loads(base64.urlsafe_b64decode(filters.cursor))
                if cursor_scope != scope or not isinstance(value, str) or not isinstance(last_id, str):
                    raise ValueError("invalid scope")
                comparison = datetime.fromisoformat(value) if filters.sort == "added" else value
                statement = statement.where(or_(
                    order_column > comparison if ascending else order_column < comparison,
                    and_(order_column == comparison, Work.id > last_id if ascending else Work.id < last_id),
                ))
            except (ValueError, TypeError) as exc:
                raise HTTPException(400, "invalid catalog cursor") from exc
        statement = statement.order_by(
            order_column.asc() if ascending else order_column.desc(),
            Work.id.asc() if ascending else Work.id.desc(),
        )
        rows = list(session.scalars(statement.limit(filters.limit + 1)))
        taxonomies = {entry.work_id: entry for entry in session.scalars(
            select(WorkTaxonomy).where(WorkTaxonomy.work_id.in_([w.id for w in rows])),
        )}
        next_cursor = None
        if len(rows) > filters.limit:
            last = rows[filters.limit - 1]
            if filters.sort == "title":
                value = last.title
            elif filters.sort in ("newest", "oldest"):
                explicit = taxonomies.get(last.id)
                value = (f"{explicit.release_year:04d}-01-01"
                         if explicit and explicit.release_year else str(last.release_date)
                         if last.release_date else "9999-12-31" if ascending else "0000-01-01")
            else:
                value = last.created_at.isoformat()
            encoded = json.dumps([scope, value, last.id]).encode()
            next_cursor = base64.urlsafe_b64encode(encoded).decode()
        poster_prefix = "/api/media/v1" if prefix != "/api/catalog" else "/api"
        cards = [_card(w, poster_prefix).model_copy(update={
            "year": taxonomies[w.id].release_year or (w.release_date.year if w.release_date else None),
        }) if w.id in taxonomies else _card(w, poster_prefix) for w in rows[:filters.limit]]
        return CatalogPage(items=cards, next_cursor=next_cursor, total=total)

    @router.post("/works/query")
    def works(filters: CatalogFilter, request: Request) -> CatalogPage:
        with runtime(request).database.session() as session:
            return query_works(filters, request, session)

    @router.get("/people")
    def list_people(request: Request, q: str = "") -> list[CatalogPerson]:
        with runtime(request).database.session() as session:
            items = people(request, session)
            return sorted(
                [p for p in items if not q or q.casefold() in " ".join([p.name, *p.aliases]).casefold()],
                key=lambda p: (-len(p.work_ids), p.name, p.id),
            )

    @router.post("/facets")
    def facets(filters: CatalogFilter, request: Request) -> dict[str, list[Facet]]:
        with runtime(request).database.session() as session:
            rows = filtered_statement(filters, request, session).subquery()
            year = func.coalesce(cast(
                select(WorkTaxonomy.release_year).where(WorkTaxonomy.work_id == rows.c.id).scalar_subquery(),
                String,
            ), func.strftime("%Y", rows.c.release_date), "unknown")
            year_counts = list(session.execute(
                select(year, func.count()).select_from(rows).group_by(year),
            ))
            decades: Counter[str] = Counter()
            for value, count in year_counts:
                decades[str(int(value) // 10 * 10) if value != "unknown" else value] += count
            tag_rows = func.json_each(rows.c.tags).table_valued("value")
            tags = list(session.execute(
                select(tag_rows.c.value, func.count(func.distinct(rows.c.id)))
                .select_from(rows).join(tag_rows, true()).group_by(tag_rows.c.value),
            ))
            organizations = list(session.execute(
                select(Collection.id, Collection.name, Collection.kind, func.count())
                .join(WorkCollection, WorkCollection.collection_id == Collection.id)
                .join(rows, rows.c.id == WorkCollection.work_id).group_by(Collection.id),
            ))
            taxonomy: dict[str, list[Facet]] = {}
            for key, column in (("genres", WorkTaxonomy.genres), ("forms", WorkTaxonomy.forms),
                                ("regions", WorkTaxonomy.regions)):
                values = func.json_each(column).table_valued("value")
                counts = session.execute(
                    select(values.c.value, func.count(func.distinct(WorkTaxonomy.work_id)))
                    .select_from(WorkTaxonomy).join(rows, rows.c.id == WorkTaxonomy.work_id)
                    .join(values, true()).group_by(values.c.value),
                )
                taxonomy[key] = [Facet(id=k, name=k, count=v) for k, v in counts]
            return {
                **taxonomy,
                "years": [Facet(id=k, name=k, count=v) for k, v in year_counts],
                "decades": [Facet(id=k, name=k, count=v) for k, v in sorted(decades.items(), reverse=True)],
                "tags": [Facet(id=k, name=k, count=v) for k, v in tags],
                "organizations": [Facet(id=k, name=n, role=r, count=v) for k, n, r, v in organizations],
            }

    if prefix == "/api/catalog":
        @router.get("/works/{work_id}/taxonomy")
        def read_taxonomy(work_id: str, request: Request) -> TaxonomyEdit:
            with runtime(request).database.session() as session:
                entry = session.get(WorkTaxonomy, work_id)
                return TaxonomyEdit(
                    genres=entry.genres, forms=entry.forms, regions=entry.regions,
                    release_year=entry.release_year,
                ) if entry else TaxonomyEdit()

        @router.put("/works/{work_id}/taxonomy")
        def edit_taxonomy(work_id: str, payload: TaxonomyEdit, request: Request) -> TaxonomyEdit:
            with runtime(request).database.session() as session:
                if session.get(Work, work_id) is None:
                    raise HTTPException(404, "work not found")
                session.merge(WorkTaxonomy(work_id=work_id, **payload.model_dump()))
            return payload

    return router
