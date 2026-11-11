"""SQL projections for explicit terms, source-derived content and missing metadata."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from fastapi import HTTPException
from sqlalchemy import String, case, exists, literal, select, union_all
from sqlalchemy.orm import Session, aliased
from sqlalchemy.sql.selectable import Subquery

from ..classifications import (
    DIMENSION_KEYS,
    DURATION_FORM_IDS,
    FAMILY_CONTENTS,
    ClassificationSelection,
    DimensionKey,
)
from ..db.models import CatalogClassificationTerm, Work, WorkClassification
from ..tag_models import TagReference


def classification_rows(dimension: str) -> Subquery:
    """One row per effective work/term pair, including a typed 'unknown' term."""
    links = (
        select(WorkClassification.work_id, WorkClassification.term_id)
        .join(
            CatalogClassificationTerm,
            CatalogClassificationTerm.id == WorkClassification.term_id,
        )
        .where(
            CatalogClassificationTerm.dimension == dimension,
            CatalogClassificationTerm.id.not_in(DURATION_FORM_IDS),
        )
    )
    source = aliased(Work)
    assigned = exists(
        select(1)
        .select_from(WorkClassification)
        .join(
            CatalogClassificationTerm,
            CatalogClassificationTerm.id == WorkClassification.term_id,
        )
        .where(
            WorkClassification.work_id == source.id,
            CatalogClassificationTerm.dimension == dimension,
            CatalogClassificationTerm.id.not_in(DURATION_FORM_IDS),
        )
    )
    fallback = (
        case(FAMILY_CONTENTS, value=source.family, else_="contents:unknown")
        if dimension == "contents"
        else literal(f"{dimension}:unknown", String)
    )
    missing = select(source.id.label("work_id"), fallback.label("term_id")).where(~assigned)
    return union_all(links, missing).subquery()


def validate_selection(session: Session, selection: ClassificationSelection) -> None:
    """Reject names, missing IDs and terms submitted under a different dimension."""
    selected = selection.selections()
    ids = {identity for values in selected.values() for identity in values}
    terms = (
        {
            term.id: term.dimension
            for term in session.scalars(
                select(CatalogClassificationTerm).where(
                    CatalogClassificationTerm.id.in_(ids),
                )
            )
        }
        if ids
        else {}
    )
    for dimension, values in selected.items():
        if any(terms.get(identity) != dimension or identity in DURATION_FORM_IDS for identity in values):
            raise HTTPException(422, f"invalid classification ID in {dimension}")


def work_classifications(
    session: Session,
    works: Sequence[Work],
) -> dict[str, dict[DimensionKey, list[TagReference]]]:
    """Batch references for a page; no query per work or inferred language/region."""
    result: dict[str, dict[DimensionKey, list[TagReference]]] = {work.id: {} for work in works}
    if not result:
        return result
    rows = session.execute(
        select(
            WorkClassification.work_id,
            CatalogClassificationTerm,
        )
        .join(CatalogClassificationTerm, CatalogClassificationTerm.id == WorkClassification.term_id)
        .where(
            WorkClassification.work_id.in_(result),
        )
        .order_by(CatalogClassificationTerm.name, CatalogClassificationTerm.id)
    )
    for work_id, term in rows:
        if term.dimension in DIMENSION_KEYS and term.id not in DURATION_FORM_IDS:
            dimension = cast(DimensionKey, term.dimension)
            result[work_id].setdefault(dimension, []).append(TagReference(term.id, term.name))
    fallback_ids = {FAMILY_CONTENTS.get(work.family, "contents:unknown") for work in works}
    fallback_names = {
        term.id: term.name
        for term in session.scalars(
            select(CatalogClassificationTerm).where(
                CatalogClassificationTerm.id.in_(fallback_ids),
            )
        )
    }
    for work in works:
        if "contents" not in result[work.id]:
            identity = FAMILY_CONTENTS.get(work.family, "contents:unknown")
            result[work.id]["contents"] = [TagReference(identity, fallback_names[identity])]
    return result
