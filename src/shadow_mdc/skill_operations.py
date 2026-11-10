"""Small, composable maintenance operations used by the operator CLI."""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import delete, select

from .classifications import ClassificationSelection
from .db.models import CatalogClassificationTerm, WorkClassification
from .db.repository import Repository
from .domain import IdentityHints
from .enums import MatchDecision, MediaCategory, QueryMode
from .identity import extract_code
from .intake_core import (
    OfflineAddRequest,
    OfflineMagnetAddRequest,
    intake_offline_add,
    intake_offline_add_magnet,
    job_out,
    normalize_code_or_422,
    require_service_token,
)
from .matching import rank_candidates
from .media.artwork import ArtworkStore
from .services.catalog_classifications import validate_selection, work_classifications
from .services.pan_offline_enqueue import OfflineEnqueueError, enqueue_work_offline
from .services.translation import WorkTranslator
from .tags import normalize_tags

if TYPE_CHECKING:
    from .api import Runtime


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkTarget(OperationInput):
    work_id: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9:_-]+$")


class TagsEdit(WorkTarget):
    add: list[str] = Field(default_factory=list, max_length=200)
    remove: list[str] = Field(default_factory=list, max_length=200)


class FillInput(WorkTarget):
    fields: list[Literal["title", "plot", "actors", "artwork", "translation"]] = Field(
        default=["actors", "artwork", "translation"], min_length=1, max_length=5
    )


class ClassificationFill(ClassificationSelection, WorkTarget):
    pass


class RecoverInput(OperationInput):
    task_id: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9:_-]+$")


class LinkInput(WorkTarget):
    url: str = Field(min_length=3, max_length=4000, pattern=r"^(magnet:\?|https?://|ed2k://)")


class IntakeItem(OperationInput):
    code: str | None = Field(default=None, min_length=2, max_length=100)
    source_url: str | None = Field(default=None, max_length=2000)
    external_ids: dict[str, str] = Field(default_factory=dict)
    magnet: str | None = Field(default=None, min_length=20, max_length=4000)
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=200)
    download: bool = False

    @model_validator(mode="after")
    def identity(self) -> IntakeItem:
        if self.magnet:
            if not self.download or self.source_url or self.external_ids:
                raise ValueError("magnet requires download=true and cannot combine with URL/external IDs")
        elif sum(bool(value) for value in (self.code, self.source_url, self.external_ids)) != 1:
            raise ValueError("provide exactly one identity")
        if self.download and not (self.code or self.magnet):
            raise ValueError("download needs code or magnet; source URLs only create metadata")
        return self


def apply_tags(repo: Repository, work_id: str, add: list[str], remove: list[str]) -> list[str]:
    """Resolve aliases and preserve tags outside the explicitly requested delta."""
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(404, "work not found")
    removed = set(normalize_tags(remove))
    tags = normalize_tags([tag for tag in normalize_tags(work.tags) if tag not in removed] + add)
    if tags != work.tags:
        repo.update_work_fields(work, tags=list(tags))
    return list(tags)


def build_operator_router(runtime: Callable[[Request], Runtime]) -> APIRouter:
    """Expose domain operations through the authenticated skill allowlist only."""
    router = APIRouter(prefix="/api/operator/v1", dependencies=[Depends(require_service_token)])

    @router.post("/tags")
    def tags(payload: TagsEdit, request: Request) -> dict[str, object]:
        with runtime(request).database.session() as session:
            result = apply_tags(Repository(session), payload.work_id, payload.add, payload.remove)
        runtime(request).response_cache.clear()
        return {"work_id": payload.work_id, "tags": result}

    @router.post("/classifications/fill")
    def classify(payload: ClassificationFill, request: Request) -> dict[str, object]:
        with runtime(request).database.session() as session:
            repo = Repository(session)
            work = repo.get_work(payload.work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            validate_selection(session, payload)
            current = work_classifications(session, [work])[work.id]
            changed: list[str] = []
            for dimension, identities in payload.selections().items():
                if dimension not in payload.model_fields_set or not identities:
                    continue
                if any(term.id != f"{dimension}:unknown" for term in current.get(dimension, [])):
                    continue
                session.execute(
                    delete(WorkClassification).where(
                        WorkClassification.work_id == work.id,
                        WorkClassification.term_id.in_(
                            select(CatalogClassificationTerm.id).where(
                                CatalogClassificationTerm.dimension == dimension,
                            )
                        ),
                    )
                )
                session.add_all(
                    WorkClassification(work_id=work.id, term_id=identity)
                    for identity in dict.fromkeys(identities)
                )
                changed.append(dimension)
        runtime(request).response_cache.clear()
        return {
            "work_id": payload.work_id,
            "changed": changed,
            "outcome": "updated" if changed else "skipped",
        }

    @router.post("/fill")
    async def fill(payload: FillInput, request: Request) -> dict[str, object]:
        app_runtime = runtime(request)
        changed: list[str] = []
        errors: list[str] = []
        with app_runtime.database.session() as session:
            repo = Repository(session)
            work = repo.get_work(payload.work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            fields = set(payload.fields) - set(work.field_locks or [])
            missing = {
                key
                for key, value in {
                    "title": work.title,
                    "plot": work.plot,
                    "actors": work.actors,
                    "artwork": work.artwork,
                }.items()
                if key in fields and not value
            }
            if missing:
                if not work.primary_code:
                    errors.append(
                        "missing fields need a refreshable code; provide verified fields with works.update"
                    )
                else:
                    code, family = extract_code(work.primary_code, MediaCategory(work.category))
                    if code:
                        hints = IdentityHints(
                            term=code,
                            code=code,
                            mode=QueryMode.CODE,
                            family=family,
                            category=MediaCategory(work.category),
                        )
                        batch = await app_runtime.providers.search(hints)
                        accepted = [
                            item.record
                            for item in rank_candidates(hints, list(batch.records))
                            if item.decision is MatchDecision.ACCEPT
                        ]
                        for record in accepted:
                            updates: dict[str, str] = {}
                            if "title" in missing and record.title:
                                updates["title"] = record.title
                            if "plot" in missing and record.plot:
                                updates["plot"] = record.plot
                            repo.update_work_fields(
                                work,
                                title=updates.get("title"),
                                plot=updates.get("plot"),
                                actors=list(record.actors) if "actors" in missing and record.actors else None,
                                lock_edited=False,
                            )
                            written = list(updates)
                            if "actors" in missing and record.actors:
                                written.append("actors")
                            if "artwork" in missing and record.artwork:
                                work.artwork = [item.model_dump(mode="json") for item in record.artwork]
                                written.append("artwork")
                            work.field_sources = {
                                **(work.field_sources or {}),
                                **dict.fromkeys(written, record.provider),
                            }
                            changed.extend(written)
                            missing -= set(written)
                        if missing:
                            errors.append("no accepted metadata for: " + ", ".join(sorted(missing)))
            if "translation" in fields:
                translate_fields = tuple(
                    key
                    for key in ("title", "plot")
                    if key not in (work.field_locks or [])
                    and not (work.field_sources or {}).get(key, "").startswith("translation:")
                )
                results = (
                    (
                        await cast(WorkTranslator, app_runtime.translator).translate_fields(
                            repo, work, fields=translate_fields
                        )
                    )
                    if translate_fields
                    else []
                )
                changed.extend(
                    "translation:" + item.field_name for item in results if item.status == "translated"
                )
                errors.extend(
                    item.detail or "translation failed" for item in results if item.status == "failed"
                )
            if "artwork" in fields and work.artwork:
                result, paths = await ArtworkStore(
                    app_runtime.settings.data_dir / "artwork",
                    app_runtime.http,
                    max_bytes=app_runtime.settings.artwork_max_bytes,
                ).acquire(work)
                if paths:
                    repo.update_artwork_local_paths(work, paths)
                if result.downloaded:
                    changed.append("artwork:cache")
                if result.failed:
                    errors.append(f"artwork downloads failed: {result.failed}")
        app_runtime.response_cache.clear()
        return {
            "work_id": payload.work_id,
            "changed": changed,
            "errors": errors,
            "outcome": "failed" if errors else "updated" if changed else "skipped",
        }

    @router.post("/recover")
    def recover(payload: RecoverInput, request: Request) -> dict[str, object]:
        # Re-adopt an existing remote result. Never submit a second remote download here.
        with runtime(request).database.session() as session:
            repo = Repository(session)
            task = repo.get_pan_offline_task(payload.task_id)
            if task is None:
                raise HTTPException(404, "offline task not found")
            if task.status == "cancelled":
                raise HTTPException(409, "cancelled task needs an explicit new intake")
            if task.strm_path and Path(task.strm_path).is_file():
                return {"outcome": "skipped", "job": job_out(task, repo.get_work(task.work_id)).model_dump()}
            repo.update_pan_offline_task(task, status="running", error=None)
            return {"outcome": "rechecking", "job": job_out(task, repo.get_work(task.work_id)).model_dump()}

    @router.post("/link")
    async def link(payload: LinkInput, request: Request) -> dict[str, object]:
        app_runtime = runtime(request)
        if payload.url.lower().startswith("ed2k:") and app_runtime.pan_service.backend() != "openlist":
            raise HTTPException(
                422, "ed2k is not supported by the direct 115 intake; catalog metadata separately"
            )
        with app_runtime.database.session() as session:
            repo = Repository(session)
            try:
                result = await enqueue_work_offline(
                    repo, app_runtime.pan_service, payload.work_id, url=payload.url
                )
            except OfflineEnqueueError as exc:
                raise HTTPException(exc.status_code, exc.message) from exc
            return {
                "outcome": "running" if result.reused_running else "submitted",
                "job": job_out(result.task, repo.get_work(payload.work_id)).model_dump(mode="json"),
            }

    @router.post("/intake")
    async def intake(payload: IntakeItem, request: Request) -> dict[str, object]:
        app_runtime = runtime(request)
        with app_runtime.database.session() as session:
            repo = Repository(session)
            if payload.download:
                if payload.magnet:
                    result = await intake_offline_add_magnet(
                        OfflineMagnetAddRequest(
                            magnet=payload.magnet, code=payload.code, title=payload.title, dry_run=False
                        ),
                        request,
                        repo,
                    )
                else:
                    result = await intake_offline_add(
                        OfflineAddRequest(code=payload.code or "", dry_run=False), request, repo
                    )
                if result.work_id:
                    apply_tags(repo, result.work_id, payload.tags, [])
                    work = repo.get_work(result.work_id)
                    if work is not None and payload.title and payload.title != work.title:
                        repo.update_work_fields(work, title=payload.title)
                app_runtime.response_cache.clear()
                return {"outcome": result.action, **result.model_dump(mode="json")}
            if payload.code:
                code = normalize_code_or_422(payload.code)
                _, family = extract_code(code)
                work = repo.find_work_by_code(code)
                hints = IdentityHints(
                    term=code, code=code, mode=QueryMode.CODE, family=family, category=MediaCategory.JAPAN
                )
            elif payload.source_url:
                work = None
                hints = IdentityHints(
                    term=payload.source_url, source_url=payload.source_url, mode=QueryMode.URL
                )
            else:
                work = next(
                    (
                        repo.find_work_by_provider_identity(provider, identity)
                        for provider, identity in payload.external_ids.items()
                        if repo.find_work_by_provider_identity(provider, identity) is not None
                    ),
                    None,
                )
                hints = IdentityHints(
                    term=next(iter(payload.external_ids.values())),
                    external_ids=payload.external_ids,
                    mode=QueryMode.EXTERNAL_ID,
                )
            created = False
            if work is None:
                batch = await app_runtime.providers.search(hints)
                accepted = [
                    item.record
                    for item in rank_candidates(hints, list(batch.records))
                    if item.decision is MatchDecision.ACCEPT
                ]
                if not accepted:
                    return {"outcome": "failed", "reason": "no accepted metadata"}
                record = accepted[0]
                existing = repo.find_work_by_provider_identity(record.provider, record.external_id)
                if existing is None and record.code:
                    existing = repo.find_work_by_code(record.code)
                created = existing is None
                work = repo.upsert_provider_record(record, overwrite=False)
            apply_tags(repo, work.id, payload.tags, [])
            if payload.title and payload.title != work.title:
                repo.update_work_fields(work, title=payload.title)
            app_runtime.response_cache.clear()
            return {
                "outcome": "created" if created else "skipped",
                "work_id": work.id,
                "code": work.primary_code,
                "title": work.title,
            }

    return router
