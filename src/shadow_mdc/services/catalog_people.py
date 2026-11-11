"""Idempotent legacy import and reversible, revision-checked person consolidation."""

from __future__ import annotations

from collections.abc import Callable
from typing import cast
from uuid import uuid4

from fastapi import HTTPException
from pydantic import BaseModel, Field, field_validator
from sqlalchemy import select, update
from sqlalchemy.dialects.sqlite import insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from ..db.models import (
    CatalogPersonCredit,
    CatalogPersonMerge,
    CatalogPersonRecord,
    CatalogPersonReference,
    Work,
)
from ..identity import IdentityAliasRules


class Collaboration(BaseModel):
    id: str
    name: str
    count: int


class PersonDocument(BaseModel):
    id: str
    ref: str
    name: str
    aliases: list[str] = Field(default_factory=list)
    biography: str | None = None
    image_url: str | None = None
    x_handle: str | None = None
    work_ids: list[str] = Field(default_factory=list)
    directed_work_ids: list[str] = Field(default_factory=list)
    excluded_work_ids: list[str] = Field(default_factory=list)
    notes: str = ""
    match_names: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    refs: list[str] = Field(default_factory=list)
    favorite: bool = False
    blacklist: bool = False
    revision: int = 1
    collaborators: list[Collaboration] = Field(default_factory=list)
    organizations: list[Collaboration] = Field(default_factory=list)
    series: list[Collaboration] = Field(default_factory=list)
    years: list[int] = Field(default_factory=list)


class PersonEdit(BaseModel):
    name: str = Field(min_length=1, max_length=300)
    aliases: list[str] = Field(default_factory=list, max_length=200)
    biography: str | None = Field(default=None, max_length=30000)
    image_url: str | None = None
    notes: str = Field(default="", max_length=30000)
    groups: list[str] = Field(default_factory=list, max_length=100)
    match_names: list[str] = Field(default_factory=list, max_length=200)
    revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def nonempty_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("name cannot be blank")
        return cleaned



class MergeRequest(BaseModel):
    target_id: str
    source_id: str
    target_revision: int
    source_revision: int


def import_people(session: Session, documents: list[PersonDocument]) -> None:
    """Each old reference owns an immutable identity, including zero-work people."""
    references = {row.ref: row.person_id for row in session.scalars(select(CatalogPersonReference))}
    for document in documents:
        person_id = references.get(document.ref, document.id)
        values = document.model_copy(update={
            "id": person_id, "work_ids": sorted(set(document.work_ids)),
        }).model_dump(mode="json")
        statement = insert(CatalogPersonRecord).values(id=person_id, document=values, edits={}, revision=1)
        session.execute(statement.on_conflict_do_update(
            index_elements=["id"], set_={"document": values},
            where=CatalogPersonRecord.document != values,
        ))
        statement_ref = insert(CatalogPersonReference).values(ref=document.ref, person_id=person_id)
        session.execute(statement_ref.on_conflict_do_nothing(index_elements=["ref"]))
    session.flush()
    session.expire_all()


def _root(person_id: str, get_record: Callable[[str], CatalogPersonRecord | None]) -> str:
    visited: set[str] = set()
    row = get_record(person_id)
    while row is not None and row.merged_into:
        if person_id in visited:
            raise HTTPException(409, "invalid person redirect")
        visited.add(person_id)
        person_id = row.merged_into
        row = get_record(person_id)
    return person_id


def people(session: Session, *, private: bool = False) -> list[PersonDocument]:
    rows = {row.id: row for row in session.scalars(select(CatalogPersonRecord))}
    known_works = set(session.scalars(select(Work.id)))
    references: dict[str, list[str]] = {}
    for ref in session.scalars(select(CatalogPersonReference)):
        references.setdefault(_root(ref.person_id, rows.get), []).append(ref.ref)
    groups: dict[str, list[CatalogPersonRecord]] = {}
    for row in rows.values():
        groups.setdefault(_root(row.id, rows.get), []).append(row)
    credits: dict[str, dict[str, set[str]]] = {}
    for credit in session.scalars(select(CatalogPersonCredit)):
        root = _root(credit.person_id, rows.get)
        credits.setdefault(root, {}).setdefault(credit.role, set()).add(credit.work_id)
    result: list[PersonDocument] = []
    for root_id, members in groups.items():
        primary = rows[root_id]
        primary_doc = PersonDocument.model_validate(primary.document)
        documents = [primary_doc, *(
            PersonDocument.model_validate(row.document) for row in members if row.id != root_id
        )]
        public_refs = sorted(set(references.get(root_id, [])))
        merged = primary_doc.model_copy(update={
            "id": root_id, "ref": public_refs[0] if public_refs else primary_doc.ref,
            "refs": public_refs,
            "aliases": sorted({name for d in documents for name in [d.name, *d.aliases]
                               if name != primary_doc.name}),
            "work_ids": sorted({work for d in documents for work in d.work_ids if work in known_works}),
            "revision": primary.revision,
        })
        if primary.edits:
            merged = PersonDocument.model_validate({**merged.model_dump(), **primary.edits})
        actor_works = set(merged.work_ids) | credits.get(root_id, {}).get("actor", set())
        merged.work_ids = sorted((actor_works - set(merged.excluded_work_ids)) & known_works)
        merged.directed_work_ids = sorted(credits.get(root_id, {}).get("director", set()) & known_works)
        if merged.name != primary_doc.name:
            merged.aliases = list(dict.fromkeys([primary_doc.name, *merged.aliases]))
        if not private:
            merged.notes = ""
            merged.match_names = []
            merged.groups = []
        result.append(merged)
    return result


def person(
    session: Session, key: str, *, private: bool = False, profiles: list[PersonDocument] | None = None,
) -> PersonDocument:
    references = session.get(CatalogPersonReference, key)
    candidate = references.person_id if references else key
    candidate = _root(candidate, lambda person_id: session.get(CatalogPersonRecord, person_id))
    catalog = profiles if profiles is not None else people(session, private=private)
    found = next((item for item in catalog if item.id == candidate), None)
    if found is None:
        name = key.removeprefix("jav:").removeprefix("nj:")
        candidates = [item for item in catalog if name == item.name or name in item.aliases]
        if len(candidates) > 1:
            raise HTTPException(409, "多位人物使用此姓名, 请从演员库选择具体档案")
        if len(candidates) == 1:
            return candidates[0]
        raise HTTPException(404, "person not found")
    return found


def edit_person(session: Session, key: str, edit: PersonEdit) -> PersonDocument:
    current = person(session, key, private=True)
    if current.revision != edit.revision:
        raise HTTPException(409, "person changed; reload before saving")
    record = session.get(CatalogPersonRecord, current.id)
    assert record is not None
    edits = {**record.edits, **edit.model_dump(exclude={"revision"})}
    changed = session.execute(update(CatalogPersonRecord).where(
        CatalogPersonRecord.id == current.id, CatalogPersonRecord.revision == edit.revision,
    ).values(edits=edits, revision=edit.revision + 1))
    if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
        raise HTTPException(409, "person changed; reload before saving")
    session.flush()
    session.expire_all()
    return person(session, current.id, private=True)


def create_person(session: Session, edit: PersonEdit) -> PersonDocument:
    key = str(uuid4())
    document = PersonDocument(
        id=key, ref=f"person:{key}", revision=1, **edit.model_dump(exclude={"revision"}),
    )
    session.add(CatalogPersonRecord(id=key, document=document.model_dump(mode="json"), edits={}))
    session.add(CatalogPersonReference(ref=document.ref, person_id=key))
    session.flush()
    return document


def merge_people(session: Session, request: MergeRequest) -> CatalogPersonMerge:
    target = person(session, request.target_id, private=True)
    source = person(session, request.source_id, private=True)
    if target.id == source.id:
        raise HTTPException(409, "people are already merged")
    for current, revision in ((target, request.target_revision), (source, request.source_revision)):
        if current.revision != revision:
            raise HTTPException(409, "person changed; reload before merging")
    # Editing either participant invalidates an undo. The source stays intact.
    for current in (target, source):
        values: dict[str, object] = {"revision": current.revision + 1}
        if current.id == source.id:
            values["merged_into"] = target.id
        changed = session.execute(update(CatalogPersonRecord).where(
            CatalogPersonRecord.id == current.id, CatalogPersonRecord.revision == current.revision,
        ).values(**values))
        if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
            raise HTTPException(409, "person changed; reload before merging")
    entry = CatalogPersonMerge(
        target_id=target.id, members=[source.id],
        revisions={target.id: target.revision + 1, source.id: source.revision + 1},
        previous_revisions={target.id: target.revision, source.id: source.revision},
    )
    session.add(entry)
    session.flush()
    return entry


def undo_merge(session: Session, merge_id: str) -> None:
    entry = session.get(CatalogPersonMerge, merge_id)
    if entry is None:
        raise HTTPException(404, "merge not found")
    if entry.undone:
        return
    for key, revision in entry.revisions.items():
        row = session.get(CatalogPersonRecord, key)
        if row is None or row.revision != revision:
            raise HTTPException(409, "person was edited or merged again; resolve later changes first")
    for key, revision in entry.revisions.items():
        changed = session.execute(update(CatalogPersonRecord).where(
            CatalogPersonRecord.id == key, CatalogPersonRecord.revision == revision,
        ).values(revision=revision + 1, **({"merged_into": None} if key in entry.members else {})))
        if cast(CursorResult[tuple[object, ...]], changed).rowcount != 1:
            raise HTTPException(409, "person changed")
    entry.undone = True
    # Reversing a later merge must permit the preceding merge to be undone in turn.
    for older in session.scalars(select(CatalogPersonMerge).where(
        CatalogPersonMerge.undone.is_(False), CatalogPersonMerge.created_at < entry.created_at,
    )):
        expected = dict(older.revisions)
        for key, before in entry.previous_revisions.items():
            if expected.get(key) == before:
                expected[key] = entry.revisions[key] + 1
        older.revisions = expected
    session.flush()


def register_renamed_reference(session: Session, old_name: str, new_name: str) -> None:
    """A user-initiated rename is explicit identity evidence."""
    old = session.get(CatalogPersonReference, f"nj:{old_name}")
    if old is None:
        return
    statement = insert(CatalogPersonReference).values(ref=f"nj:{new_name}", person_id=old.person_id)
    session.execute(statement.on_conflict_do_nothing(index_elements=["ref"]))


def enrich_catalog_aliases(session: Session, rules: IdentityAliasRules) -> IdentityAliasRules:
    """Make manually maintained directory hints available to the normal scanner."""
    profiles = people(session, private=True)
    claims: dict[str, set[str]] = {}
    names: dict[str, str] = {}
    for profile in profiles:
        names[profile.id] = profile.name
        for alias in [profile.name, *profile.aliases, *profile.match_names]:
            if alias.strip():
                claims.setdefault(alias.strip(), set()).add(profile.id)
    aliases = dict(rules.actors)
    for alias, ids in claims.items():
        if len(ids) == 1:
            aliases.setdefault(alias, names[next(iter(ids))])
    return rules.model_copy(update={"actors": aliases})
