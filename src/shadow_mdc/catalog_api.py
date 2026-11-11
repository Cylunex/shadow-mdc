"""Unified catalog projection shared by the Web and authenticated mobile views."""

from __future__ import annotations

import base64
import hashlib
import json
import unicodedata
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from io import BytesIO
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Annotated, Literal
from typing import cast as type_cast
from uuid import NAMESPACE_URL, uuid4, uuid5
from weakref import WeakKeyDictionary

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.params import Depends
from fastapi.responses import FileResponse
from PIL import Image, UnidentifiedImageError
from pydantic import BaseModel, Field
from sqlalchemy import (
    Integer,
    Select,
    SQLColumnExpression,
    String,
    and_,
    case,
    cast,
    delete,
    event,
    exists,
    func,
    or_,
    select,
    text,
    true,
    update,
)
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult, Engine
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session
from sqlalchemy.sql.elements import ColumnElement

from .catalog_duration import DURATION_BANDS, duration_condition, work_duration
from .catalog_management import CatalogFilter, build_management_router, organization_relationships
from .catalog_series import find_series, series_relationships
from .classifications import DURATION_FORM_IDS, ClassificationSelection, classification_dimensions
from .db.models import (
    Actor,
    CatalogClassificationTerm,
    CatalogPersonCredit,
    CatalogPersonMerge,
    CatalogPersonRecord,
    CatalogSeries,
    Collection,
    MediaAsset,
    MediaEmbyBinding,
    Work,
    WorkActor,
    WorkClassification,
    WorkCollection,
    WorkTaxonomy,
)
from .media.artwork import resolve_work_display_images
from .services.actor_images import actor_images, valid_key
from .services.catalog_classifications import classification_rows, validate_selection, work_classifications
from .services.catalog_people import (
    Collaboration,
    MergeRequest,
    PersonDocument,
    PersonEdit,
    create_person,
    edit_person,
    import_people,
    merge_people,
    undo_merge,
)
from .services.catalog_people import (
    people as stored_people,
)
from .services.catalog_people import (
    person as stored_person,
)
from .services.javranking_client import JavRankingIndexCache
from .services.library_prefs import LibraryPrefs
from .tag_models import TagReference, fold_tag
from .tags import current_registry, normalize_tags, resolve_tag, tag_refs

if TYPE_CHECKING:
    from .api import Runtime



_imported_sources: WeakKeyDictionary[Engine, tuple[int, tuple[tuple[int, int], ...]]] = WeakKeyDictionary()
_imported_sources_lock = Lock()


class PersonWorkEdit(BaseModel):
    work_id: str
    role: Literal["actor", "director"] = "actor"
    included: bool = True
    revision: int


class PersonPreference(BaseModel):
    favorite: bool = False
    blacklist: bool = False


class TaxonomyEdit(ClassificationSelection):
    release_year: int | None = Field(default=None, ge=1800, le=2200)


class TaxonomyOut(TaxonomyEdit):
    classification_refs: dict[str, list[TagReference]] = Field(default_factory=dict)


class ClassificationTermEdit(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=100)
    revision: int = Field(default=1, ge=1)


class ClassificationTermCreate(ClassificationTermEdit):
    dimension: str


class ClassificationTermOut(ClassificationTermEdit):
    id: str
    count: int = 0


class ClassificationDimensionOut(BaseModel):
    id: str
    name: str
    description: str
    values: list[ClassificationTermOut]


class ClassificationsOut(BaseModel):
    dimensions: list[ClassificationDimensionOut]
    legacy_terms: dict[str, list[ClassificationTermOut]] = Field(default_factory=dict)


class CatalogCard(BaseModel):
    id: str
    title: str
    code: str | None
    year: int | None
    actors: list[str]
    tags: list[str]
    tag_refs: list[TagReference] = Field(default_factory=list)
    classification_refs: dict[str, list[TagReference]] = Field(default_factory=dict)
    studio: str | None
    label: str | None
    series: str | None
    overview: str | None = None
    duration_ms: int | None
    poster: str
    category: str = "Other"
    release_date: str | None = None
    has_local_media: bool = False
    rating_value: float | None = None
    rating_max: float | None = None
    javranking_rank: int | None = None
    javranking_score: float | None = None


class CatalogPage(BaseModel):
    items: list[CatalogCard]
    next_cursor: str | None
    total: int


CatalogPerson = PersonDocument


class PersonCard(BaseModel):
    id: str
    name: str
    aliases: list[str]
    image_url: str | None
    work_count: int
    revision: int
    same_name: bool


class PeoplePage(BaseModel):
    items: list[PersonCard]
    next_offset: int | None
    total: int = 0
    labels: dict[str, str] = Field(default_factory=dict)


class Facet(BaseModel):
    id: str
    name: str
    count: int
    role: str = ""


class TagFacet(Facet):
    aliases: list[str] = Field(default_factory=list)
    group: str = ""
    parent_id: str | None = None
    path: list[TagReference] = Field(default_factory=list)
    redirect_ids: tuple[str, ...] = ()


def _card(work: Work, prefix: str, *, detail: bool = False) -> CatalogCard:
    return CatalogCard(
        id=work.id, title=work.title, code=work.primary_code,
        year=work.release_date.year if work.release_date else None,
        actors=work.actors, tags=list(normalize_tags(work.tags)), tag_refs=tag_refs(work.tags),
        studio=work.studio,
        label=work.label, series=work.series, overview=work.plot if detail else None,
        category=work.category, release_date=str(work.release_date) if work.release_date else None,
        rating_value=work.rating_value, rating_max=work.rating_max,
        duration_ms=work.runtime_seconds * 1000 if work.runtime_seconds else None,
        poster=(f"/api/works/{work.id}/artwork/poster" if prefix == "/api"
                    else f"{prefix}/works/{work.id}/poster"),
    )


def _legacy_people(runtime_state: Runtime, session: Session) -> list[CatalogPerson]:
    relations: dict[str, list[str]] = {}
    for actor_id, work_id in session.execute(select(WorkActor.actor_id, WorkActor.work_id)):
        relations.setdefault(actor_id, []).append(work_id)
    result = {
        actor.id: CatalogPerson(id=actor.id, ref=f"jav:{actor.id}", name=actor.name,
                                aliases=actor.aliases, image_url=actor.image_url, x_handle=actor.x_handle,
                                work_ids=relations.get(actor.id, []))
        for actor in session.scalars(select(Actor))
    }
    for profile in runtime_state.actor_store.load():
        key = profile.id or str(uuid5(NAMESPACE_URL, f"mdc:actor:{profile.name}"))
        if key in result:
            result[key].work_ids = sorted(set(result[key].work_ids) | {w.id for w in profile.works})
            result[key].aliases = sorted(set(result[key].aliases) | set(profile.aliases))
            result[key].image_url = result[key].image_url or profile.image_url
            result[key].x_handle = result[key].x_handle or profile.x_handle
            continue
        result[key] = CatalogPerson(
            id=key, ref=f"jav:{profile.id or profile.name}", name=profile.name,
            aliases=list(profile.aliases), image_url=profile.image_url, x_handle=profile.x_handle,
            work_ids=[w.id for w in profile.works],
        )
    # Keep curated names in their original namespace, including zero-work entries.
    # Reuse the existing matching rules rather than merging names across catalogs.
    name_index: dict[str, set[str]] = {}
    for work_id, names in session.execute(select(Work.id, Work.actors)):
        for name in names:
            normalized = unicodedata.normalize("NFKC", name).casefold().strip()
            name_index.setdefault(normalized, set()).add(work_id)
    for curated in runtime_state.non_jav_actor_store.load().actors:
        key = str(uuid5(NAMESPACE_URL, f"mdc:curated:{curated.name}"))
        result[key] = CatalogPerson(
            id=key, ref=f"nj:{curated.name}", name=curated.name,
            aliases=list(curated.aliases), biography=curated.biography, x_handle=curated.x_handle,
            notes=curated.notes or "", match_names=list(curated.match_names),
            groups=list(curated.groups),
            image_url=f"/api/non-jav-actor-images/{curated.image_file}" if curated.image_file else None,
            work_ids=sorted(set().union(*(name_index.get(
                unicodedata.normalize("NFKC", name).casefold().strip(), set(),
            ) for name in [curated.name, *curated.aliases, *curated.match_names]))),
        )
    return list(result.values())

def _sync_people(runtime_state: Runtime, session: Session) -> None:
    engine = runtime_state.database.engine
    revision = session.scalar(text(
        "SELECT revision FROM catalog_source_revisions WHERE name = 'people'"
    )) or 0
    data_dir = runtime_state.settings.data_dir
    files = [data_dir / "actor-catalog.json", data_dir / "non-jav-actors.json"]
    signatures = tuple((path.stat().st_mtime_ns, path.stat().st_size) if path.exists()
                       else (0, 0) for path in files)
    signature = (int(revision), signatures)
    with _imported_sources_lock:
        imported = _imported_sources.get(engine)
    if imported != signature and session.info.get("catalog_signature") != signature:
        import_people(session, _legacy_people(runtime_state, session))
        session.info["catalog_signature"] = signature

        def committed(_session: Session) -> None:
            with _imported_sources_lock:
                _imported_sources[engine] = signature

        event.listen(session, "after_commit", committed, once=True)


def _person_tags(person: CatalogPerson, prefs: LibraryPrefs) -> tuple[bool, bool]:
    keys = [person.id, *[ref.split(":", 1)[-1] for ref in person.refs]]
    states = [prefs.actor_tags[key] for key in keys if key in prefs.actor_tags]
    return any(state.favorite for state in states), any(state.blacklist for state in states)


def _person_index(profiles: list[CatalogPerson]) -> dict[str, CatalogPerson]:
    index = {
        key: p for p in profiles for key in [*p.refs, *[ref.split(":", 1)[-1] for ref in p.refs]]
    }
    index.update({p.id: p for p in profiles})
    return index


def read_catalog_people(
    runtime_state: Runtime, session: Session, *, private: bool = False,
) -> list[CatalogPerson]:
    _sync_people(runtime_state, session)
    items = stored_people(session, private=private)
    if private:
        prefs = runtime_state.library_prefs_store.load()
        for item in items:
            item.image_url = actor_images().display_url(item.image_url)
            item.favorite, item.blacklist = _person_tags(item, prefs)
    if not private:
        return [item.model_copy(update={
            "image_url": f"/api/media/v1/catalog/people/{item.id}/image",
        }) for item in items]
    return items


def catalog_credit_names(
    runtime_state: Runtime, session: Session, works: list[Work],
) -> dict[str, list[str]]:
    work_ids = {work.id for work in works}
    overrides = session.scalar(select(exists().where(CatalogPersonRecord.edits != {})))
    explicit = session.scalar(select(exists().where(CatalogPersonCredit.work_id.in_(work_ids))))
    if not overrides and not explicit:
        return {}
    profiles = read_catalog_people(runtime_state, session)
    names: dict[str, list[str]] = {}
    for work in works:
        original = {name: position for position, name in enumerate(work.actors)}
        entries: list[tuple[int, str]] = []
        known_aliases: set[str] = set()
        for profile in profiles:
            if work.id not in profile.work_ids and work.id not in profile.excluded_work_ids:
                continue
            aliases = {profile.name, *profile.aliases}
            known_aliases.update(aliases)
            if work.id in profile.work_ids:
                position = min((original[alias] for alias in aliases if alias in original), default=1000000)
                entries.append((position, profile.name))
        # Preserve legacy text credits that have not yet acquired an entity relationship.
        entries.extend((position, name) for name, position in original.items() if name not in known_aliases)
        names[work.id] = [name for _, name in sorted(entries)]
    return names


def build_catalog_router(
    runtime: Callable[[Request], Runtime], *, prefix: str = "/api/catalog",
    dependencies: list[Depends] | None = None,
) -> APIRouter:
    """Use a compatibility projection so legacy identities and preferences remain addressable."""
    router = APIRouter(prefix=prefix, dependencies=dependencies)

    def people(request: Request, session: Session) -> list[CatalogPerson]:
        return read_catalog_people(runtime(request), session, private=prefix == "/api/catalog")

    def filtered_statement(
        filters: CatalogFilter, request: Request, session: Session,
        actor_profiles: dict[str, CatalogPerson] | None = None,
    ) -> Select[tuple[Work]]:
        statement = select(Work)
        if filters.category != "all":
            statement = statement.where(Work.category == filters.category)
        if filters.collection:
            kind, _, name = filters.collection.partition(":")
            canonical_series = find_series(session, name) if kind == "series" else None
            if canonical_series is not None:
                memberships = series_relationships()
                statement = statement.where(Work.id.in_(select(memberships.c.work_id).where(
                    memberships.c.series_id == canonical_series.id,
                )))
            else:
                statement = statement.where(Work.id.in_(select(WorkCollection.work_id).join(
                    Collection, Collection.id == WorkCollection.collection_id,
                ).where(Collection.kind.in_(("studio", "organization") if kind == "studio" else (kind,)),
                        Collection.name == name)))
        if filters.q.strip():
            q = filters.q.strip()
            profiles_for_search = (list(actor_profiles.values()) if actor_profiles is not None
                                   else people(request, session))
            actor_work_ids = {work_id for person in profiles_for_search
                              if q.casefold() in " ".join([person.name, *person.aliases]).casefold()
                              for work_id in person.work_ids}
            links_for_search = organization_relationships()
            organization_matches = select(links_for_search.c.work_id).join(
                Collection, Collection.id == links_for_search.c.organization_id,
            ).where(or_(Collection.name.icontains(q, autoescape=True),
                        cast(Collection.aliases, String).icontains(q, autoescape=True)))
            memberships = series_relationships()
            series_matches = select(memberships.c.work_id).join(
                CatalogSeries, CatalogSeries.id == memberships.c.series_id,
            ).where(or_(CatalogSeries.name.icontains(q, autoescape=True),
                        cast(CatalogSeries.aliases, String).icontains(q, autoescape=True)))
            statement = statement.where(or_(
                Work.title.icontains(q, autoescape=True), Work.primary_code.icontains(q, autoescape=True),
                cast(Work.actors, String).icontains(q, autoescape=True),
                Work.studio.icontains(q, autoescape=True), Work.label.icontains(q, autoescape=True),
                Work.series.icontains(q, autoescape=True),
                Work.id.in_(actor_work_ids), Work.id.in_(organization_matches), Work.id.in_(series_matches),
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
        if filters.series_name:
            canonical_series = find_series(session, filters.series_name)
            if canonical_series is not None:
                memberships = series_relationships()
                statement = statement.where(Work.id.in_(select(memberships.c.work_id).where(
                    memberships.c.series_id == canonical_series.id,
                )))
            else:
                statement = statement.where(Work.series == filters.series_name)
        if filters.series:
            memberships = series_relationships()
            statement = statement.where(Work.id.in_(select(memberships.c.work_id).where(
                memberships.c.series_id.in_(filters.series),
            )))
        if filters.actors:
            profiles = (actor_profiles if actor_profiles is not None
                        else _person_index(people(request, session)))
            groups = [set(profiles[key].work_ids) if key in profiles else set() for key in filters.actors]
            ids = set.intersection(*groups) if filters.actors_all else set.union(*groups)
            statement = statement.where(Work.id.in_(ids))
        if filters.tags or filters.exclude_tags or filters.genres:
            # SQLite json_each avoids materializing every work in Python.
            def tagged(tag: str) -> ColumnElement[bool]:
                values = func.json_each(Work.tags).table_valued("value")
                return exists(select(1).select_from(values).where(func.mdc_tag_matches(values.c.value, tag)))
            predicates = [tagged(tag) for tag in filters.tags]
            if predicates:
                statement = (statement.where(*predicates) if filters.tags_all
                             else statement.where(or_(*predicates)))
            for tag in filters.exclude_tags:
                statement = statement.where(~tagged(tag))
            if filters.genres:
                terms = list(session.scalars(select(CatalogClassificationTerm).where(
                    CatalogClassificationTerm.dimension == "genres",
                )))
                lookup = {fold_tag(value): term for term in terms
                          for value in (term.id, term.name, *term.aliases)}
                genre_predicates: list[ColumnElement[bool]] = []
                for value in filters.genres:
                    if value == "genres:unknown":
                        known = {term.tag_id for term in terms if term.tag_id}
                        known.update(tag.id for tag in current_registry().definitions
                                     if current_registry().in_branch(tag.id, "题材"))
                        genre_predicates.append(~or_(*(tagged(identity) for identity in known)) if known
                                                else true())
                        continue
                    term = lookup.get(fold_tag(value))
                    resolved_genre = current_registry().resolve(value)
                    identity = term.tag_id if term is not None else (
                        resolved_genre.id
                        if resolved_genre is not None
                        and current_registry().in_branch(resolved_genre.id, "题材")
                        else None
                    )
                    if identity is None:
                        raise HTTPException(422, "invalid legacy genre ID")
                    genre_predicates.append(tagged(identity))
                statement = statement.where(or_(*genre_predicates))
        legacy_forms = [identity for identity in filters.forms if identity in DURATION_FORM_IDS]
        active = filters.model_copy(update={
            "forms": [identity for identity in filters.forms if identity not in DURATION_FORM_IDS],
        })
        validate_selection(session, active)
        for dimension, selected in active.selections().items():
            if dimension == "forms" and legacy_forms:
                # Historical form selections retain OR semantics; new menus use duration.
                values = classification_rows("forms")
                statement = statement.where(or_(
                    Work.id.in_(select(WorkClassification.work_id).where(
                        WorkClassification.term_id.in_(legacy_forms),
                    )),
                    exists(select(1).select_from(values).where(
                        values.c.work_id == Work.id, values.c.term_id.in_(selected),
                    )),
                ))
                continue
            if selected:
                values = classification_rows(dimension)
                statement = statement.where(exists(select(1).select_from(values).where(
                    values.c.work_id == Work.id, values.c.term_id.in_(selected),
                )))
        if filters.subtitle_languages:
            valid = set(session.scalars(select(CatalogClassificationTerm.id).where(
                CatalogClassificationTerm.dimension == "subtitle_languages",
            )))
            if any(identity not in valid for identity in filters.subtitle_languages):
                raise HTTPException(422, "invalid legacy subtitle ID")
            values = classification_rows("subtitle_languages")
            statement = statement.where(exists(select(1).select_from(values).where(
                values.c.work_id == Work.id, values.c.term_id.in_(filters.subtitle_languages),
            )))
        duration = work_duration()
        if filters.unknown_duration:
            statement = statement.where(duration.is_(None))
        if filters.duration_min is not None:
            statement = statement.where(duration >= filters.duration_min)
        if filters.duration_max is not None:
            statement = statement.where(duration < filters.duration_max)
        if filters.organizations:
            links = organization_relationships()
            matching = select(links.c.work_id).where(
                links.c.organization_id.in_(filters.organizations),
            )
            if filters.organization_role:
                matching = matching.where(links.c.role == filters.organization_role)
            condition: ColumnElement[bool] = Work.id.in_(matching)
            # Old clients saved series Collection IDs under "organizations".
            # Adapt those memberships without exposing series as organizations.
            if filters.organization_role in ("", "series"):
                memberships = series_relationships()
                condition = or_(condition, Work.id.in_(select(memberships.c.work_id).where(
                    memberships.c.series_id.in_(filters.organizations),
                )))
            statement = statement.where(condition)
        for identities, kind in ((filters.labels, "label"), (filters.platforms, "platform")):
            if identities:
                links = organization_relationships()
                statement = statement.where(Work.id.in_(select(links.c.work_id).join(
                    Collection, Collection.id == links.c.organization_id,
                ).where(links.c.organization_id.in_(identities), Collection.kind == kind)))
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
        if filters.wanted_filter is not None:
            wanted = Work.id.in_(runtime(request).library_prefs_store.load().want_list)
            statement = statement.where(wanted if filters.wanted_filter else ~wanted)
        if filters.media_availability == "undownloaded":
            statement = statement.where(~files)
        elif filters.media_availability == "files":
            statement = statement.where(files)
        elif filters.media_availability == "emby":
            statement = statement.where(emby)
        elif filters.media_availability == "metadata":
            statement = statement.where(~files, ~emby)
        return statement

    def work_order(
        filters: CatalogFilter, request: Request,
    ) -> tuple[SQLColumnExpression[str] | SQLColumnExpression[datetime], bool]:
        order_column: SQLColumnExpression[str] | SQLColumnExpression[datetime]
        if filters.sort == "ranked":
            honors = JavRankingIndexCache(runtime(request).settings.data_dir / "javranking").read_honors_map()
            # Fixed-width keys keep cursor ordering stable, including unranked works.
            order_column = case({code: f"{info.rank or 999999999:09d}" for code, info in honors.items()},
                                value=Work.primary_code, else_="999999999") if honors else cast(
                                    "999999999", String,
                                )
            ascending = True
        elif filters.sort == "random":
            order_column = func.substr(Work.id, filters.seed % 24 + 1)
            ascending = True
        elif filters.sort == "code":
            order_column = func.coalesce(Work.primary_code, "")
            ascending = True
        elif filters.sort == "title":
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
        return order_column, ascending

    def ordered_playlist_works(
        filters: CatalogFilter, request: Request, session: Session,
    ) -> Select[tuple[Work]]:
        column, ascending = work_order(filters, request)
        return filtered_statement(filters, request, session).order_by(
            column.asc() if ascending else column.desc(),
            Work.id.asc() if ascending else Work.id.desc(),
        )

    def query_works(filters: CatalogFilter, request: Request, session: Session) -> CatalogPage:
        statement = filtered_statement(filters, request, session)
        total = session.scalar(select(func.count()).select_from(statement.subquery())) or 0
        order_column, ascending = work_order(filters, request)
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
            if filters.sort in ("title", "code", "random", "ranked"):
                value = str(session.scalar(select(order_column).where(Work.id == last.id)))
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
        names = catalog_credit_names(runtime(request), session, rows[:filters.limit])
        cards = [_card(w, poster_prefix).model_copy(update={
            "year": taxonomies[w.id].release_year or (w.release_date.year if w.release_date else None),
        }) if w.id in taxonomies else _card(w, poster_prefix) for w in rows[:filters.limit]]
        refs = work_classifications(session, rows[:filters.limit])
        cards = [card.model_copy(update={"classification_refs": refs[card.id]}) for card in cards]
        cards = [card.model_copy(update={"actors": names[card.id]})
                 if card.id in names else card for card in cards]
        if prefix == "/api/catalog":
            images = resolve_work_display_images(
                rows[:filters.limit], data_dir=runtime(request).settings.data_dir,
            )
            local_ids = set(session.scalars(select(MediaAsset.work_id).where(
                MediaAsset.work_id.in_([card.id for card in cards]),
            )))
            ranking = JavRankingIndexCache(runtime(request).settings.data_dir / "javranking")
            for card in cards:
                card.poster = images.get(card.id) or ""
                card.has_local_media = card.id in local_ids
                honor = ranking.lookup_honors(card.code)
                if honor:
                    card.javranking_rank, card.javranking_score = honor.rank, honor.score
        return CatalogPage(items=cards, next_cursor=next_cursor, total=total)

    @router.post("/works/query")
    def works(filters: CatalogFilter, request: Request) -> CatalogPage:
        with runtime(request).database.session() as session:
            return query_works(filters, request, session)

    @router.get("/people")
    def list_people(request: Request, q: str = "", work_id: str | None = None) -> list[CatalogPerson]:
        with runtime(request).database.session() as session:
            items = people(request, session)
            return sorted(
                [p for p in items
                 if (not q or q.casefold() in " ".join([p.name, *p.aliases, *p.refs]).casefold())
                 and (work_id is None or work_id in p.work_ids or work_id in p.directed_work_ids)],
                key=lambda p: (-len(p.work_ids), p.name, p.id),
            )

    if prefix == "/api/catalog":
        @router.get("/people-cards")
        def person_cards(
            request: Request, q: str = Query(default="", max_length=300),
            sort: Literal["works", "name"] = "works", followed: bool = False,
            offset: int = Query(default=0, ge=0),
            limit: int = Query(default=48, ge=1, le=100),
            ids: Annotated[list[str] | None, Query(alias="id", max_length=100)] = None,
        ) -> PeoplePage:
            runtime_state = runtime(request)
            with runtime_state.database.session() as session:
                _sync_people(runtime_state, session)
                profiles = stored_people(session)
                if ids:
                    index = _person_index(profiles)
                    return PeoplePage(
                        items=[], next_offset=None,
                        labels={key: index[key].name for key in ids if key in index},
                    )
                names = Counter(person.name for person in profiles)
                query = q.strip().casefold()
                prefs = runtime_state.library_prefs_store.load() if followed else None
                matches = [
                    person for person in profiles
                    if (not query or any(query in name.casefold() for name in [person.name, *person.aliases]))
                    and (prefs is None or _person_tags(person, prefs)[0])
                ]
                matches.sort(key=lambda p: (
                    -len(p.work_ids) if sort == "works" else 0, p.name.casefold(), p.id,
                ))
                return PeoplePage(
                    items=[PersonCard(
                        id=p.id, name=p.name, aliases=p.aliases[:2],
                        image_url=actor_images().display_url(p.image_url), work_count=len(p.work_ids),
                        revision=p.revision, same_name=names[p.name] > 1,
                    ) for p in matches[offset:offset + limit]],
                    next_offset=offset + limit if offset + limit < len(matches) else None,
                    total=len(matches),
                )

    @router.post("/facets")
    def facets(filters: CatalogFilter, request: Request) -> dict[str, list[Facet | TagFacet]]:
        with runtime(request).database.session() as session:
            actor_profiles = _person_index(people(request, session)) if filters.actors or filters.q else None
            year_filters = filters.model_copy(update={
                "year_from": None, "year_to": None, "unknown_year": False,
            })
            rows = filtered_statement(year_filters, request, session, actor_profiles).subquery()
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
            tag_filters = filters if filters.tags_all else filters.model_copy(update={"tags": []})
            tag_base = filtered_statement(tag_filters, request, session, actor_profiles).subquery()
            tag_rows = func.json_each(tag_base.c.tags).table_valued("value")
            ancestors = func.json_each(func.mdc_tag_tree_ids(tag_rows.c.value)).table_valued("value")
            identity = ancestors.c.value
            tags = list(session.execute(
                select(identity, func.min(tag_rows.c.value), func.count(func.distinct(tag_base.c.id)))
                .select_from(tag_base).join(tag_rows, true()).join(ancestors, true())
                .where(identity.is_not(None))
                .group_by(identity),
            ))
            tag_counts = {key: count for key, _raw, count in tags}
            tag_facets = {
                key: TagFacet(id=key, name=tag.name, count=count, aliases=list(tag.aliases), group=tag.group,
                              parent_id=tag.parent_id, path=list(current_registry().path(tag.id)),
                              redirect_ids=current_registry().redirect_ids(tag.id))
                for key, raw, count in tags
                if (tag := resolve_tag(key if key in current_registry().ids else raw)) is not None
            }
            # Keep selected/excluded labels readable even when other filters hide all their works.
            for raw in [*filters.tags, *filters.exclude_tags]:
                tag = resolve_tag(raw)
                if tag is None:
                    explicit = current_registry().resolve(raw)
                    tag = explicit if explicit is not None and explicit.id in current_registry().ids else None
                if tag is not None:
                    for ancestor_id in current_registry().ancestor_ids(tag.id):
                        node = tag if ancestor_id == tag.id else resolve_tag(ancestor_id)
                        if node is not None and ancestor_id not in tag_facets:
                            tag_facets[ancestor_id] = TagFacet(
                                id=node.id, name=node.name, count=tag_counts.get(node.id, 0),
                                aliases=list(node.aliases),
                                group=node.group,
                                parent_id=node.parent_id, path=list(current_registry().path(node.id)),
                                redirect_ids=current_registry().redirect_ids(node.id),
                            )
            organization_filters = filters.model_copy(update={"organizations": [], "organization_role": ""})
            organization_rows = filtered_statement(
                organization_filters, request, session, actor_profiles,
            ).subquery()
            relationships = organization_relationships()
            organizations = list(session.execute(
                select(Collection.id, Collection.name, Collection.kind,
                       func.count(func.distinct(relationships.c.work_id)))
                .join(relationships, relationships.c.organization_id == Collection.id)
                .join(organization_rows, organization_rows.c.id == relationships.c.work_id)
                .group_by(Collection.id),
            ))
            organization_facets: dict[str, list[Facet]] = {}
            for field, kind in (("labels", "label"), ("platforms", "platform")):
                matching_rows = filtered_statement(filters.model_copy(update={field: []}), request,
                                                   session, actor_profiles).subquery()
                credit_counts = session.execute(select(
                    Collection.id, Collection.name, func.count(func.distinct(relationships.c.work_id)),
                ).join(relationships, relationships.c.organization_id == Collection.id)
                    .join(matching_rows, matching_rows.c.id == relationships.c.work_id)
                    .where(Collection.kind == kind).group_by(Collection.id)).all()
                organization_facets[field] = [Facet(id=k, name=n, role=kind, count=v)
                                              for k, n, v in credit_counts]
            memberships = series_relationships()
            series_rows = filtered_statement(filters.model_copy(update={"series": []}), request,
                                             session, actor_profiles).subquery()
            series_facets = session.execute(select(
                CatalogSeries.id, CatalogSeries.name, func.count(func.distinct(memberships.c.work_id)),
            ).join(memberships, memberships.c.series_id == CatalogSeries.id)
                .join(series_rows, series_rows.c.id == memberships.c.work_id)
                .group_by(CatalogSeries.id)).all()
            taxonomy: dict[str, list[Facet | TagFacet]] = {}
            terms = {term.id: term for term in session.scalars(select(CatalogClassificationTerm))}
            for dimension in classification_dimensions():
                values = classification_rows(dimension.id)
                dimension_rows = filtered_statement(
                    filters.model_copy(update={dimension.id: []}), request, session, actor_profiles,
                ).subquery()
                counts: dict[str, int] = {identity: count for identity, count in session.execute(select(
                    values.c.term_id, func.count(func.distinct(values.c.work_id)),
                ).join(dimension_rows, dimension_rows.c.id == values.c.work_id).group_by(values.c.term_id))}
                for selected in filters.selections()[dimension.id]:
                    if selected not in DURATION_FORM_IDS:
                        counts.setdefault(selected, 0)
                taxonomy[dimension.id] = [TagFacet(
                    id=identity, name=terms[identity].name, count=count, aliases=terms[identity].aliases,
                ) for identity, count in counts.items()]
            collection_base = filtered_statement(filters.model_copy(update={"collection": ""}), request,
                                                 session, actor_profiles).subquery()
            collections = session.execute(select(Collection.kind, Collection.name, func.count()).join(
                WorkCollection, WorkCollection.collection_id == Collection.id,
            ).join(collection_base, collection_base.c.id == WorkCollection.work_id).group_by(
                Collection.kind, Collection.name,
            )).all()
            duration_base = filtered_statement(filters.model_copy(update={
                "duration_min": None, "duration_max": None, "unknown_duration": False,
            }), request, session, actor_profiles).subquery()
            runtime_seconds = duration_base.c.runtime_seconds
            counts_by_band = session.execute(select(*(
                func.count(case((duration_condition(runtime_seconds, band), 1))) for band in DURATION_BANDS
            )).select_from(duration_base)).one()
            duration_counts = [Facet(id=band.id, name=band.name, count=count)
                               for band, count in zip(DURATION_BANDS, counts_by_band, strict=True)]
            legacy_facets = {dimension: [Facet(id=identity, name=terms[identity].name, count=0)
                                        for identity in selected if identity in terms]
                             for dimension, selected in (("genres", filters.genres),
                                                         ("subtitle_languages", filters.subtitle_languages))}
            # Older mobile clients still ask for genres; these choices read the
            # same grouped tags, without restoring a separate genre vocabulary.
            grouped_tags = {tag.id: tag for tag in tag_facets.values()
                            if current_registry().in_branch(tag.id, "题材")}
            for facet in legacy_facets["genres"]:
                term = terms[facet.id]
                tag_facet = tag_facets.get(term.tag_id or "")
                if tag_facet is not None:
                    grouped_tags[facet.id] = tag_facet.model_copy(update={"id": facet.id})
            legacy_facets["genres"] = list(grouped_tags.values())
            return {
                "collections": [Facet(id=f"{kind}:{name}", name=name, count=count)
                                for kind, name, count in collections],
                **taxonomy,
                "years": [Facet(id=k, name=k, count=v) for k, v in year_counts],
                "decades": [Facet(id=k, name=k, count=v) for k, v in sorted(decades.items(), reverse=True)],
                "tags": sorted(tag_facets.values(), key=lambda tag: (-tag.count, tag.name.casefold())),
                "organizations": [Facet(id=k, name=n, role=r, count=v) for k, n, r, v in organizations
                                  if r in ("organization", "studio")],
                **organization_facets,
                "series": [Facet(id=k, name=n, count=v) for k, n, v in series_facets],
                "durations": duration_counts,
                **legacy_facets,
            }

    @router.get("/people/{person_id}/image")
    async def person_image(person_id: str, request: Request) -> FileResponse:
        with runtime(request).database.session() as session:
            _sync_people(runtime(request), session)
            profile = stored_person(session, person_id)
        url = actor_images().display_url(profile.image_url) or ""
        lazy_prefix = "/api/actor-images/remote/"
        if url.startswith(lazy_prefix) and valid_key(url[len(lazy_prefix):]):
            path = await actor_images().localize(url[len(lazy_prefix):])
            if path is not None:
                return FileResponse(path)
        for route in ("/api/actor-images/", "/api/non-jav-actor-images/"):
            if url.startswith(route):
                name = url[len(route):].split("?", 1)[0]
                if Path(name).name == name and name not in ("", ".", ".."):
                    path = runtime(request).settings.data_dir / "actor-images" / name
                    if path.is_file():
                        return FileResponse(path)
        raise HTTPException(404, "portrait is not locally available")

    @router.get("/people/{person_id}")
    def person_detail(person_id: str, request: Request) -> CatalogPerson:
        with runtime(request).database.session() as session:
            catalog = people(request, session)
            result = stored_person(session, person_id, profiles=catalog)
            ids = set(result.work_ids)
            collaborators = []
            for other in catalog:
                count = len(ids.intersection(other.work_ids))
                if other.id != result.id and count:
                    collaborators.append(Collaboration(id=other.id, name=other.name, count=count))
            result.collaborators = sorted(collaborators, key=lambda item: (-item.count, item.name))[:20]
            result.years = sorted({
                work.release_date.year for work in session.scalars(select(Work).where(Work.id.in_(ids)))
                if work.release_date
            }, reverse=True)
            relationships = organization_relationships()
            linked = session.execute(select(
                Collection.id, Collection.name, func.count(func.distinct(relationships.c.work_id)),
            ).join(relationships, relationships.c.organization_id == Collection.id).where(
                relationships.c.work_id.in_(ids),
            ).group_by(Collection.id))
            result.organizations = [Collaboration(id=key, name=name, count=count)
                                    for key, name, count in linked]
            memberships = series_relationships()
            linked_series = session.execute(select(
                CatalogSeries.id, CatalogSeries.name, func.count(func.distinct(memberships.c.work_id)),
            ).join(memberships, memberships.c.series_id == CatalogSeries.id)
                .where(memberships.c.work_id.in_(ids)).group_by(CatalogSeries.id))
            result.series = [Collaboration(id=key, name=name, count=count)
                             for key, name, count in linked_series]
            return result

    if prefix == "/api/catalog":
        @router.put("/people/{person_id}/works")
        def edit_credit(person_id: str, payload: PersonWorkEdit, request: Request) -> CatalogPerson:
            with runtime(request).database.session() as session:
                people(request, session)
                current = stored_person(session, person_id, private=True)
                if session.get(Work, payload.work_id) is None:
                    raise HTTPException(404, "work not found")
                record = session.get(CatalogPersonRecord, current.id)
                assert record is not None
                excluded = set(current.excluded_work_ids)
                if payload.role == "actor":
                    if payload.included:
                        excluded.discard(payload.work_id)
                    else:
                        excluded.add(payload.work_id)
                changed = session.execute(update(CatalogPersonRecord).where(
                    CatalogPersonRecord.id == current.id, CatalogPersonRecord.revision == payload.revision,
                ).values(edits={**record.edits, "excluded_work_ids": sorted(excluded)},
                         revision=payload.revision + 1))
                if type_cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
                    raise HTTPException(409, "person changed; reload")
                if payload.included:
                    statement = insert(CatalogPersonCredit).values(
                        person_id=current.id, work_id=payload.work_id, role=payload.role,
                    )
                    session.execute(statement.on_conflict_do_nothing())
                else:
                    session.execute(delete(CatalogPersonCredit).where(
                        CatalogPersonCredit.person_id == current.id,
                        CatalogPersonCredit.work_id == payload.work_id,
                        CatalogPersonCredit.role == payload.role,
                    ))
                session.flush()
                session.expire_all()
                return stored_person(session, current.id, private=True)

        @router.put("/people/{person_id}/preference")
        def person_preference(
            person_id: str, payload: PersonPreference, request: Request,
        ) -> PersonPreference:
            with runtime(request).database.session() as session:
                people(request, session)
                current = stored_person(session, person_id)
                keys = [current.id, *[ref.split(":", 1)[-1] for ref in current.refs]]
                runtime(request).library_prefs_store.set_actor_tag_group(
                    keys, favorite=payload.favorite, blacklist=payload.blacklist,
                )
            return payload

        @router.get("/migration")
        def migration_report(request: Request) -> dict[str, int]:
            with runtime(request).database.session() as session:
                before = len(stored_people(session))
                migrated = people(request, session)
                return {"people": len(migrated), "new_people": len(migrated) - before,
                        "works": session.scalar(select(func.count()).select_from(Work)) or 0,
                        "emby_bindings": (
                            session.scalar(select(func.count()).select_from(MediaEmbyBinding)) or 0
                        )}

        @router.post("/people/{person_id}/image")
        async def upload_person_image(
            person_id: str, revision: int, request: Request,
        ) -> CatalogPerson:
            content = bytearray()
            async for chunk in request.stream():
                content.extend(chunk)
                if len(content) > 5 * 1024 * 1024:
                    raise HTTPException(413, "portrait must be at most 5 MiB")
            extension = (".png" if content.startswith(b"\x89PNG\r\n\x1a\n") else ".jpg"
                         if content.startswith(b"\xff\xd8\xff") else ".webp"
                         if content.startswith(b"RIFF") and content[8:12] == b"WEBP" else None)
            if extension is None:
                raise HTTPException(415, "only PNG, JPEG and WebP portraits are supported")
            try:
                with Image.open(BytesIO(content)) as image:
                    if image.width * image.height > 25_000_000:
                        raise HTTPException(413, "portrait dimensions exceed the limit")
                    image.verify()
            except (UnidentifiedImageError, OSError, ValueError) as exc:
                raise HTTPException(415, "invalid image") from exc
            with runtime(request).database.session() as session:
                people(request, session)
                current = stored_person(session, person_id, private=True)
                if current.revision != revision:
                    raise HTTPException(409, "person changed; reload")
                filename = "person-" + hashlib.sha256(content).hexdigest() + extension
                directory = runtime(request).settings.data_dir / "actor-images"
                directory.mkdir(parents=True, exist_ok=True)
                path = directory / filename
                if not path.exists():
                    temporary = path.with_suffix(path.suffix + "." + uuid4().hex + ".tmp")
                    try:
                        temporary.write_bytes(content)
                        temporary.replace(path)
                    finally:
                        temporary.unlink(missing_ok=True)
                edit = PersonEdit(
                    name=current.name, aliases=current.aliases, biography=current.biography,
                    image_url="/api/actor-images/" + filename, notes=current.notes, groups=current.groups,
                    match_names=current.match_names, revision=revision,
                )
                return edit_person(session, current.id, edit)

        @router.post("/people", status_code=201)
        def add_person(payload: PersonEdit, request: Request) -> CatalogPerson:
            with runtime(request).database.session() as session:
                return create_person(session, payload)

        @router.put("/people/{person_id}")
        def update_person(person_id: str, payload: PersonEdit, request: Request) -> CatalogPerson:
            with runtime(request).database.session() as session:
                people(request, session)
                return edit_person(session, person_id, payload)

        @router.post("/people-merge")
        def merge_person(payload: MergeRequest, request: Request) -> dict[str, str]:
            with runtime(request).database.session() as session:
                people(request, session)
                entry = merge_people(session, payload)
                return {"merge_id": entry.id}

        @router.get("/people-merges")
        def person_merges(request: Request) -> list[dict[str, object]]:
            with runtime(request).database.session() as session:
                return [{"id": entry.id, "target_id": entry.target_id,
                         "members": entry.members, "undone": entry.undone}
                        for entry in session.scalars(select(CatalogPersonMerge).order_by(
                            CatalogPersonMerge.created_at.desc(),
                        ).limit(100))]

        @router.post("/people-merges/{merge_id}/undo", status_code=204)
        def undo_person_merge(merge_id: str, request: Request) -> None:
            with runtime(request).database.session() as session:
                undo_merge(session, merge_id)

    def taxonomy_document(work_id: str, session: Session) -> TaxonomyOut:
        work = session.get(Work, work_id)
        if work is None:
            raise HTTPException(404, "work not found")
        refs = work_classifications(session, [work])[work.id]
        year = session.get(WorkTaxonomy, work_id)
        return TaxonomyOut(
            **{key: [term.id for term in values] for key, values in refs.items()},
            release_year=year.release_year if year else None,
            classification_refs={key: value for key, value in refs.items()},
        )

    @router.get("/works/{work_id}/taxonomy")
    def read_taxonomy(work_id: str, request: Request) -> TaxonomyOut:
        with runtime(request).database.session() as session:
            return taxonomy_document(work_id, session)

    @router.get("/classifications")
    def classifications(request: Request) -> ClassificationsOut:
        counts = facets(CatalogFilter(), request)
        with runtime(request).database.session() as session:
            terms = list(session.scalars(select(CatalogClassificationTerm).order_by(
                CatalogClassificationTerm.name, CatalogClassificationTerm.id,
            )))
            dimensions = [ClassificationDimensionOut(
                id=dimension.id, name=dimension.name, description=dimension.description,
                values=[ClassificationTermOut(
                    id=term.id, name=term.name, aliases=term.aliases, revision=term.revision,
                    count=next((facet.count for facet in counts[dimension.id] if facet.id == term.id), 0),
                ) for term in terms if term.dimension == dimension.id and term.id not in DURATION_FORM_IDS],
            ) for dimension in classification_dimensions()]
            dimensions.append(ClassificationDimensionOut(
                id="decades", name="年代", description="按发行年归类、支持年代与其它维度组合筛选。",
                values=[ClassificationTermOut(
                    id=f"decades:{facet.id}",
                    name="年代待补" if facet.id == "unknown" else f"{facet.id} 年代",
                    count=facet.count,
                ) for facet in counts["decades"]],
            ))
            legacy_terms = {dimension: [ClassificationTermOut(
                id=term.id, name=term.name, aliases=term.aliases, revision=term.revision,
            ) for term in terms if term.dimension == dimension
                and (dimension != "forms" or term.id in DURATION_FORM_IDS)]
                for dimension in ("genres", "subtitle_languages", "forms")}
            return ClassificationsOut(dimensions=dimensions, legacy_terms=legacy_terms)

    if prefix == "/api/catalog":
        @router.put("/works/{work_id}/taxonomy")
        def edit_taxonomy(work_id: str, payload: TaxonomyEdit, request: Request) -> TaxonomyOut:
            with runtime(request).database.session() as session:
                if session.get(Work, work_id) is None:
                    raise HTTPException(404, "work not found")
                validate_selection(session, payload)
                for dimension, identities in payload.selections().items():
                    if dimension not in payload.model_fields_set:
                        continue
                    session.execute(delete(WorkClassification).where(
                        WorkClassification.work_id == work_id,
                        WorkClassification.term_id.in_(select(CatalogClassificationTerm.id).where(
                            CatalogClassificationTerm.dimension == dimension,
                            CatalogClassificationTerm.id.not_in(DURATION_FORM_IDS),
                        )),
                    ))
                    session.add_all([WorkClassification(work_id=work_id, term_id=identity)
                                     for identity in dict.fromkeys(identities)])
                if "release_year" in payload.model_fields_set:
                    session.merge(WorkTaxonomy(work_id=work_id, release_year=payload.release_year))
                session.flush()
                return taxonomy_document(work_id, session)

        def check_term_name(
            session: Session, dimension: str, payload: ClassificationTermEdit, identity: str = "",
        ) -> None:
            if dimension not in {item.id for item in classification_dimensions()}:
                raise HTTPException(422, "invalid classification dimension")
            names = {fold_tag(name) for name in [payload.name, *payload.aliases]}
            if "" in names:
                raise HTTPException(422, "classification names cannot be blank")
            for term in session.scalars(select(CatalogClassificationTerm).where(
                CatalogClassificationTerm.dimension == dimension, CatalogClassificationTerm.id != identity,
            )):
                if names.intersection(fold_tag(name) for name in [term.name, *term.aliases]):
                    raise HTTPException(409, "classification name or alias already exists")

        @router.post("/classifications/terms", status_code=201)
        def create_term(payload: ClassificationTermCreate, request: Request) -> ClassificationTermOut:
            with runtime(request).database.session() as session:
                check_term_name(session, payload.dimension, payload)
                term = CatalogClassificationTerm(
                    id=f"{payload.dimension}:custom-{uuid4()}", dimension=payload.dimension,
                    name=payload.name.strip(), aliases=[name.strip() for name in payload.aliases], revision=1,
                    normalized_name=fold_tag(payload.name),
                )
                session.add(term)
                try:
                    session.flush()
                except IntegrityError as exc:
                    raise HTTPException(409, "classification name already exists") from exc
                return ClassificationTermOut(
                    id=term.id, name=term.name, aliases=term.aliases, revision=term.revision,
                )

        @router.put("/classifications/terms/{term_id}")
        def edit_term(
            term_id: str, payload: ClassificationTermEdit, request: Request,
        ) -> ClassificationTermOut:
            with runtime(request).database.session() as session:
                term = session.get(CatalogClassificationTerm, term_id)
                if term is None:
                    raise HTTPException(404, "classification not found")
                check_term_name(session, term.dimension, payload, term_id)
                requested = payload.aliases if "aliases" in payload.model_fields_set else term.aliases
                aliases = list(dict.fromkeys([*requested, term.name]))
                try:
                    changed = session.execute(update(CatalogClassificationTerm).where(
                        CatalogClassificationTerm.id == term_id,
                        CatalogClassificationTerm.revision == payload.revision,
                    ).values(name=payload.name.strip(), normalized_name=fold_tag(payload.name),
                             aliases=aliases, revision=payload.revision + 1))
                except IntegrityError as exc:
                    raise HTTPException(409, "classification name already exists") from exc
                if type_cast(CursorResult[tuple[()]], changed).rowcount != 1:
                    raise HTTPException(409, "classification revision conflict")
                return ClassificationTermOut(
                    id=term_id, name=payload.name.strip(), aliases=aliases, revision=payload.revision + 1,
                )

    router.include_router(build_management_router(
        runtime, ordered_playlist_works, prefix="", writable=prefix == "/api/catalog",
    ))
    return router
