"""Editable vocabulary shared by the web UI and the authenticated operator skill."""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from .db.models import Work
from .services.tag_dictionary import UNCHANGED_PARENT, TagRevisionConflict
from .tag_models import TagReference, TagRules, TagTerm, fold_tag
from .tags import is_noise_tag

if TYPE_CHECKING:
    from .api import Runtime


class TagEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    aliases: tuple[str, ...] | None = Field(default=None, max_length=200)
    hidden: bool | None = None
    group: str | None = Field(default=None, max_length=100)
    parent_id: str | None = Field(default=None, max_length=4000)
    revision: int = Field(ge=1)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("name cannot be blank")
        return value.strip()


class TagUpdate(TagEdit):
    tag_id: str = Field(min_length=1, max_length=4000)


class TagCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=200)
    hidden: bool = False
    group: str = Field(default="", max_length=100)
    parent_id: str | None = Field(default=None, max_length=4000)

    @field_validator("name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        return TagEdit.nonempty(value)


class TagOut(TagTerm):
    count: int = 0
    direct_count: int = 0
    path: list[TagReference] = Field(default_factory=list)


class TagNode(BaseModel):
    id: str
    name: str
    parent_id: str | None
    aliases: tuple[str, ...]
    hidden: bool


class TagTreeOut(BaseModel):
    nodes: list[TagNode]
    revision: int


class TagsOut(BaseModel):
    terms: list[TagOut]
    total: int
    revision: int


class RulesEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    rules: TagRules
    revision: int = Field(ge=1)


class RulesOut(RulesEdit):
    pass


def build_tag_dictionary_router(runtime: Callable[[Request], Runtime]) -> APIRouter:
    router = APIRouter(prefix="/api/catalog/tags")

    @router.get("")
    def listing(
        request: Request,
        q: str = Query(default="", max_length=300),
        offset: int = Query(default=0, ge=0),
        limit: int = Query(default=100, ge=1, le=500),
    ) -> TagsOut:
        store = runtime(request).tag_dictionary
        document = store.load()
        registry = store.registry()
        by_id = {
            term.id: TagOut(**term.model_dump(), path=list(registry.path(term.id))) for term in document.terms
        }
        counts: Counter[str] = Counter()
        direct_counts: Counter[str] = Counter()
        with runtime(request).database.session() as session:
            for work_tags in session.scalars(select(Work.tags)).yield_per(1000):
                direct: set[str] = set()
                ancestors: set[str] = set()
                for raw in work_tags or ():
                    tag = registry.resolve(str(raw))
                    if tag is None:
                        continue
                    direct.add(tag.id)
                    ancestors.update(registry.ancestor_ids(tag.id))
                    if tag.id not in by_id:
                        by_id[tag.id] = TagOut(
                            id=tag.id,
                            name=tag.name,
                            hidden=is_noise_tag(str(raw)),
                            group=tag.group,
                            parent_id=tag.parent_id,
                        )
                counts.update(ancestors)
                direct_counts.update(direct)
        terms = [
            term.model_copy(update={"count": counts[term.id], "direct_count": direct_counts[term.id]})
            for term in by_id.values()
            if not q or any(fold_tag(q) in fold_tag(value) for value in (term.name, term.id, *term.aliases))
        ]
        terms.sort(key=lambda term: (fold_tag(term.name), term.id))
        return TagsOut(terms=terms[offset : offset + limit], total=len(terms), revision=document.revision)

    @router.get("/tree")
    def tree(request: Request) -> TagTreeOut:
        store = runtime(request).tag_dictionary
        registry = store.registry()
        return TagTreeOut(
            nodes=[
                TagNode(
                    id=tag.id, name=tag.name, parent_id=tag.parent_id, aliases=tag.aliases, hidden=tag.hidden
                )
                for tag in sorted(
                    registry.definitions,
                    key=lambda node: tuple(fold_tag(part.name) for part in registry.path(node.id)),
                )
            ],
            revision=store.load().revision,
        )

    @router.get("/rules")
    def rules(request: Request) -> RulesOut:
        document = runtime(request).tag_dictionary.load()
        return RulesOut(rules=document.rules, revision=document.revision)

    @router.put("/rules")
    def put_rules(payload: RulesEdit, request: Request) -> RulesOut:
        try:
            document = runtime(request).tag_dictionary.update_rules(payload.rules, payload.revision)
        except TagRevisionConflict as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime(request).response_cache.clear()
        return RulesOut(rules=document.rules, revision=document.revision)

    @router.post("", status_code=201)
    def create(payload: TagCreate, request: Request) -> TagTerm:
        try:
            term = runtime(request).tag_dictionary.create(
                payload.name,
                payload.aliases,
                payload.hidden,
                payload.group,
                parent_id=payload.parent_id,
            )
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime(request).response_cache.clear()
        return term

    @router.put("")
    def update(payload: TagUpdate, request: Request) -> TagTerm:
        try:
            term = runtime(request).tag_dictionary.update(
                payload.tag_id,
                payload.name,
                payload.aliases,
                payload.hidden,
                payload.revision,
                payload.group,
                parent_id=payload.parent_id if "parent_id" in payload.model_fields_set else UNCHANGED_PARENT,
            )
        except KeyError as exc:
            raise HTTPException(404, "tag not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime(request).response_cache.clear()
        return term

    return router
