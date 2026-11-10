"""Transactional, one-way migration from legacy taxonomy arrays to term relations."""

from __future__ import annotations

import json
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import Connection, Engine, select, update
from sqlalchemy.dialects.sqlite import insert

from ..classifications import DEFINITION_ALIASES, DEFINITIONS, DIMENSION_KEYS
from ..tag_registry import fold_tag
from .models import CatalogClassificationTerm, CatalogPlaylistRule, Work, WorkClassification


def _migrated_identity(connection: Connection, dimension: str, raw: str) -> str:
    name = raw.strip()
    existing = connection.scalar(
        select(CatalogClassificationTerm.id).where(
            CatalogClassificationTerm.id == name,
            CatalogClassificationTerm.dimension == dimension,
        )
    )
    if existing:
        return str(existing)
    definition = DEFINITION_ALIASES.get((dimension, fold_tag(name)))
    if definition:
        return definition.id
    existing = connection.scalar(
        select(CatalogClassificationTerm.id).where(
            CatalogClassificationTerm.dimension == dimension,
            CatalogClassificationTerm.normalized_name == fold_tag(name),
        )
    )
    if existing:
        return str(existing)
    identity = f"{dimension}:custom-{uuid5(NAMESPACE_URL, f'shadow-mdc:{dimension}:{fold_tag(name)}')}"
    connection.execute(
        insert(CatalogClassificationTerm)
        .values(
            id=identity,
            dimension=dimension,
            name=name,
            normalized_name=fold_tag(name),
            aliases=[],
            revision=1,
        )
        .on_conflict_do_nothing(index_elements=["id"])
    )
    return identity


def _legacy_values(encoded: object, dimension: str) -> list[str]:
    values = json.loads(encoded) if isinstance(encoded, str) else encoded
    if not isinstance(values, list) or any(not isinstance(value, str) for value in values):
        raise ValueError(f"Invalid legacy classification array: {dimension}")
    return [value.strip() for value in values if value.strip()]


def migrate_classifications(engine: Engine) -> None:
    """Seed terms, migrate assignments and playlist rules, verify, then drop old columns."""
    with engine.begin() as connection:
        for term in DEFINITIONS:
            connection.execute(
                insert(CatalogClassificationTerm)
                .values(
                    id=term.id,
                    dimension=term.dimension,
                    name=term.name,
                    normalized_name=fold_tag(term.name),
                    aliases=list(term.aliases),
                    revision=1,
                )
                .on_conflict_do_nothing(index_elements=["id"])
            )
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(work_taxonomy)")}
        legacy = [key for key in ("genres", "forms", "regions") if key in columns]
        expected: set[tuple[str, str]] = set()
        if legacy:
            rows = connection.exec_driver_sql("SELECT work_id, " + ", ".join(legacy) + " FROM work_taxonomy")
            work_ids = set(connection.scalars(select(Work.id)))
            for row in rows:
                work_id = str(row[0])
                if work_id not in work_ids:
                    raise ValueError("Legacy classification references a missing work")
                for dimension, encoded in zip(legacy, row[1:], strict=True):
                    for name in _legacy_values(encoded, dimension):
                        expected.add((work_id, _migrated_identity(connection, dimension, name)))
            for work_id, identity in sorted(expected):
                connection.execute(
                    insert(WorkClassification)
                    .values(
                        work_id=work_id,
                        term_id=identity,
                    )
                    .on_conflict_do_nothing()
                )
            actual = set(connection.execute(select(WorkClassification.work_id, WorkClassification.term_id)))
            if not expected.issubset(actual):
                raise ValueError("Classification migration verification failed")
        for playlist_id, filters in connection.execute(
            select(
                CatalogPlaylistRule.playlist_id,
                CatalogPlaylistRule.filters,
            )
        ):
            migrated = dict(filters)
            for dimension in DIMENSION_KEYS:
                if dimension in filters:
                    migrated[dimension] = list(
                        dict.fromkeys(
                            _migrated_identity(connection, dimension, name)
                            for name in _legacy_values(filters[dimension], dimension)
                        )
                    )
            if migrated != filters:
                connection.execute(
                    update(CatalogPlaylistRule)
                    .where(
                        CatalogPlaylistRule.playlist_id == playlist_id,
                    )
                    .values(filters=migrated)
                )
        for column in legacy:
            connection.exec_driver_sql(f"ALTER TABLE work_taxonomy DROP COLUMN {column}")
