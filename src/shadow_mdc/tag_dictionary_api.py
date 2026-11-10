"""Editable vocabulary shared by the web UI and the authenticated operator skill."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Callable
from typing import TYPE_CHECKING

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import func, select, true

from .db.models import Work
from .services.tag_dictionary import TagRevisionConflict
from .tag_models import TagRules, TagTerm, fold_tag
from .tags import is_noise_tag

if TYPE_CHECKING:
    from .api import Runtime


class TagEdit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=300)
    aliases: tuple[str, ...] | None = Field(default=None, max_length=200)
    hidden: bool | None = None
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

    @field_validator("name")
    @classmethod
    def nonempty(cls, value: str) -> str:
        return TagEdit.nonempty(value)


class TagOut(TagTerm):
    count: int = 0


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
        by_id = {term.id: TagOut(**term.model_dump()) for term in document.terms}
        counts: defaultdict[str, set[str]] = defaultdict(set)
        with runtime(request).database.session() as session:
            values = func.json_each(Work.tags).table_valued("value").alias("tags")
            for work_id, raw in session.execute(
                select(Work.id, values.c.value).select_from(Work).join(values, true())
            ):
                tag = registry.resolve(str(raw))
                if tag is None:
                    continue
                counts[tag.id].add(work_id)
                if tag.id not in by_id:
                    by_id[tag.id] = TagOut(id=tag.id, name=tag.name, hidden=is_noise_tag(str(raw)))
        terms = [
            term.model_copy(update={"count": len(counts[term.id])})
            for term in by_id.values()
            if not q or any(fold_tag(q) in fold_tag(value) for value in (term.name, term.id, *term.aliases))
        ]
        terms.sort(key=lambda term: (fold_tag(term.name), term.id))
        return TagsOut(terms=terms[offset : offset + limit], total=len(terms), revision=document.revision)

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
            term = runtime(request).tag_dictionary.create(payload.name, payload.aliases, payload.hidden)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime(request).response_cache.clear()
        return term

    @router.put("")
    def update(payload: TagUpdate, request: Request) -> TagTerm:
        try:
            term = runtime(request).tag_dictionary.update(
                payload.tag_id, payload.name, payload.aliases, payload.hidden, payload.revision
            )
        except KeyError as exc:
            raise HTTPException(404, "tag not found") from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        runtime(request).response_cache.clear()
        return term

    return router
