"""One-way migration of legacy genre relations into the unified tag store."""

from dataclasses import dataclass

from sqlalchemy import Engine, delete, select, update

from ..db.models import CatalogClassificationTerm, Work, WorkClassification
from .tag_dictionary import ImportedTag, TagDictionaryStore


@dataclass
class GenreWorkMigration:
    tags: list[str]
    locks: list[str]
    sources: dict[str, str]
    has_genre: bool = False


def migrate_genres_to_tags(engine: Engine, store: TagDictionaryStore) -> None:
    """Copy genre relations into the tag store, verify, then retire only copied relations.

    Dictionary writes happen first and are idempotent. A DB rollback may leave unused
    dictionary terms, but never loses assignments or duplicates them on retry.
    """
    with engine.begin() as connection:
        terms = (
            connection.execute(
                select(CatalogClassificationTerm).where(
                    CatalogClassificationTerm.dimension == "genres",
                    CatalogClassificationTerm.id != "genres:unknown",
                )
            )
            .mappings()
            .all()
        )
        identities = store.merge_imports(
            [
                ImportedTag(str(term["id"]), str(term["name"]), tuple(term["aliases"]), "题材")
                for term in terms
                if term["tag_id"] is None
            ]
        )
        for term in terms:
            identity = identities.get(str(term["id"]), term["tag_id"])
            connection.execute(
                update(CatalogClassificationTerm)
                .where(
                    CatalogClassificationTerm.id == term["id"],
                )
                .values(tag_id=identity)
            )
            identities[str(term["id"])] = str(identity)
        registry = store.registry()
        rows = connection.execute(
            select(
                WorkClassification.work_id,
                WorkClassification.term_id,
                Work.tags,
                Work.field_locks,
                Work.field_sources,
            )
            .join(Work, Work.id == WorkClassification.work_id)
            .join(
                CatalogClassificationTerm,
                CatalogClassificationTerm.id == WorkClassification.term_id,
            )
            .where(CatalogClassificationTerm.dimension == "genres")
        ).all()
        by_work: dict[str, GenreWorkMigration] = {}
        for work_id, term_id, tags, locks, sources in rows:
            plan = by_work.setdefault(work_id, GenreWorkMigration(list(tags), list(locks), dict(sources)))
            if term_id == "genres:unknown":
                continue
            identity = identities[term_id]
            plan.has_genre = True
            if not any(
                (tag := registry.resolve(value)) is not None and tag.id == identity for value in plan.tags
            ):
                plan.tags.append(identity)
        for work_id, plan in by_work.items():
            if plan.has_genre:
                if "tags" not in plan.locks:
                    plan.locks.append("tags")
                plan.sources["tags"] = "manual"
            connection.execute(
                update(Work)
                .where(Work.id == work_id)
                .values(
                    tags=plan.tags,
                    field_locks=plan.locks,
                    field_sources=plan.sources,
                )
            )
            if connection.scalar(select(Work.tags).where(Work.id == work_id)) != plan.tags:
                raise ValueError("genre tag migration verification failed")
        connection.execute(
            delete(WorkClassification).where(
                WorkClassification.term_id.in_(
                    select(CatalogClassificationTerm.id).where(
                        CatalogClassificationTerm.dimension == "genres",
                    )
                ),
            )
        )
