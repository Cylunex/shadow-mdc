"""Series dossiers and membership, with a projection for old Collection clients."""

from __future__ import annotations

from sqlalchemy import Engine, delete, exists, func, select, union
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.orm import Session
from sqlalchemy.sql.selectable import Subquery

from .collection_names import normalize_collection_name
from .db.models import (
    CatalogOrganization,
    CatalogSeries,
    CatalogWorkOrganization,
    CatalogWorkSeries,
    Collection,
    Work,
    WorkCollection,
)
from .enums import OrganizationKind

ORGANIZATION_KINDS = tuple(OrganizationKind)


def find_series(session: Session, name: str) -> CatalogSeries | None:
    """An old credited name remains an alias after a dossier rename."""
    normalized = normalize_collection_name(name)
    row = session.scalar(select(CatalogSeries).where(CatalogSeries.normalized_name == normalized))
    if row is None:
        aliases = func.json_each(CatalogSeries.aliases).table_valued("value")
        row = session.scalar(
            select(CatalogSeries).where(
                exists(
                    select(1).select_from(aliases).where(aliases.c.value == name.strip()),
                )
            )
        )
    return row


def ensure_series(session: Session, name: str) -> CatalogSeries:
    """Resolve a series identity without inferring its owner from its name."""
    normalized = normalize_collection_name(name)
    row = session.scalar(select(CatalogSeries).where(CatalogSeries.normalized_name == normalized))
    if row is not None:
        return row
    legacy = session.scalar(
        select(Collection).where(
            Collection.kind == "series",
            Collection.normalized_name == normalized,
        )
    )
    if legacy is None:
        row = find_series(session, name)
        if row is not None:
            return row
        legacy = Collection(name=name.strip(), normalized_name=normalized, kind="series")
        session.add(legacy)
        session.flush()
    extra = session.get(CatalogOrganization, legacy.id)
    parent = session.get(Collection, extra.parent_id) if extra and extra.parent_id else None
    row = CatalogSeries(
        id=legacy.id,
        name=legacy.name,
        normalized_name=normalized,
        aliases=extra.aliases if extra else legacy.aliases,
        description=extra.description if extra else legacy.description,
        organization_id=parent.id if parent and parent.kind in ORGANIZATION_KINDS else None,
        revision=extra.revision if extra else 0,
    )
    session.add(row)
    session.flush()
    return row


def series_relationships() -> Subquery:
    """Membership includes old IDs, but never organization production credits."""
    return union(
        select(CatalogWorkSeries.series_id, CatalogWorkSeries.work_id),
        select(WorkCollection.collection_id.label("series_id"), WorkCollection.work_id)
        .join(Collection, Collection.id == WorkCollection.collection_id)
        .where(Collection.kind == "series"),
    ).subquery()


def link_series(session: Session, work_id: str, series_id: str, *, source: str = "manual") -> None:
    session.execute(
        insert(CatalogWorkSeries)
        .values(
            work_id=work_id,
            series_id=series_id,
            source=source,
        )
        .on_conflict_do_nothing()
    )
    session.execute(
        insert(WorkCollection)
        .values(
            work_id=work_id,
            collection_id=series_id,
        )
        .on_conflict_do_nothing()
    )


def sync_field_series(session: Session, work: Work) -> CatalogSeries | None:
    """Replace only field-derived membership; keep explicitly maintained memberships."""
    row = ensure_series(session, work.series) if work.series else None
    old = list(
        session.scalars(
            select(CatalogWorkSeries).where(
                CatalogWorkSeries.work_id == work.id,
                CatalogWorkSeries.source == "field",
            )
        )
    )
    for link in old:
        if row is not None and link.series_id == row.id:
            continue
        session.delete(link)
        session.flush()
        other = session.scalar(
            select(CatalogWorkSeries).where(
                CatalogWorkSeries.work_id == work.id,
                CatalogWorkSeries.series_id == link.series_id,
            )
        )
        if other is None:
            session.execute(
                delete(WorkCollection).where(
                    WorkCollection.work_id == work.id,
                    WorkCollection.collection_id == link.series_id,
                )
            )
    if row is not None:
        link_series(session, work.id, row.id, source="field")
    return row


def migrate_series(engine: Engine) -> None:
    """Split legacy series once, retaining IDs, dossiers, manual credits and unknown ownership."""
    with engine.begin() as connection:
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        connection.exec_driver_sql(
            "CREATE TABLE IF NOT EXISTS catalog_entity_migrations (name TEXT PRIMARY KEY)"
        )
        if connection.exec_driver_sql(
            "SELECT name FROM catalog_entity_migrations WHERE name = 'separate_series_v1'"
        ).first():
            return
        with Session(bind=connection) as session:
            # Old seeded maker rows were all called "studio". Only a maintained
            # organization dossier is evidence for that subtype. Keep every ID.
            for legacy in session.scalars(
                select(Collection).where(
                    Collection.kind == "studio",
                    ~exists(select(1).where(CatalogOrganization.id == Collection.id)),
                )
            ):
                duplicate = session.scalar(
                    select(Collection.id).where(
                        Collection.kind == "organization",
                        Collection.normalized_name == legacy.normalized_name,
                    )
                )
                if duplicate is None:
                    legacy.kind = "organization"
            session.flush()
            for work_id, identity, name, maker in session.execute(
                select(
                    WorkCollection.work_id,
                    Collection.id,
                    Collection.name,
                    Work.studio,
                )
                .join(Collection, Collection.id == WorkCollection.collection_id)
                .join(Work, Work.id == WorkCollection.work_id)
                .where(Collection.kind.in_(ORGANIZATION_KINDS))
            ).all():
                if maker and normalize_collection_name(maker) == normalize_collection_name(name):
                    session.execute(
                        insert(CatalogWorkOrganization)
                        .values(
                            organization_id=identity,
                            work_id=work_id,
                            role="studio",
                        )
                        .on_conflict_do_nothing()
                    )
            for legacy in list(session.scalars(select(Collection).where(Collection.kind == "series"))):
                ensure_series(session, legacy.name)
            for work_id, collection_id, name, field in session.execute(
                select(WorkCollection.work_id, Collection.id, Collection.name, Work.series)
                .join(Collection, Collection.id == WorkCollection.collection_id)
                .join(Work, Work.id == WorkCollection.work_id)
                .where(Collection.kind == "series"),
            ).all():
                link_series(
                    session,
                    work_id,
                    collection_id,
                    source="field"
                    if field and normalize_collection_name(field) == normalize_collection_name(name)
                    else "legacy",
                )
            for work_id, name in session.execute(
                select(CatalogWorkOrganization.work_id, Collection.name)
                .join(Collection, Collection.id == CatalogWorkOrganization.organization_id)
                .where(CatalogWorkOrganization.role == "series"),
            ).all():
                link_series(session, work_id, ensure_series(session, name).id)
            for work in session.scalars(select(Work).where(Work.series.is_not(None))):
                if not work.series:
                    continue
                sync_field_series(session, work)
                # Old name detection promoted series to platforms. Remove only the
                # derived link for a name absent from both organization fields.
                normalized = normalize_collection_name(work.series)
                organization_names = {
                    normalize_collection_name(value) for value in (work.studio, work.label) if value
                }
                if normalized not in organization_names:
                    session.execute(
                        delete(WorkCollection).where(
                            WorkCollection.work_id == work.id,
                            WorkCollection.collection_id.in_(
                                select(Collection.id).where(
                                    Collection.kind == "platform",
                                    Collection.normalized_name == normalized,
                                )
                            ),
                        )
                    )
            session.flush()
        connection.exec_driver_sql("INSERT INTO catalog_entity_migrations VALUES ('separate_series_v1')")
