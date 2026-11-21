"""Small, composable maintenance operations used by the operator CLI."""

from __future__ import annotations

from collections.abc import Callable, Set
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Literal, cast

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy import and_, delete, exists, func, or_, select

from .api_models import OfflineDirectory
from .catalog_gaps import gap_conditions
from .classification import category_for_family
from .classifications import DURATION_FORM_IDS, ClassificationSelection
from .db.models import (
    CatalogClassificationTerm,
    CatalogPersonCredit,
    CatalogWorkOrganization,
    CatalogWorkSeries,
    ExternalIdentity,
    MediaAsset,
    MediaEmbyBinding,
    PanOfflineTask,
    SourceSnapshot,
    Work,
    WorkActor,
    WorkClassification,
    WorkMagnet,
    WorkTaxonomy,
)
from .db.repository import Repository
from .domain import IdentityHints
from .enums import MatchDecision, MediaCategory, QueryMode
from .identity import extract_code
from .intake_core import (
    MAGNET_INGEST_PROVIDER,
    MAGNET_INGEST_TAGS,
    OfflineAddRequest,
    OfflineMagnetAddRequest,
    intake_offline_add,
    intake_offline_add_magnet,
    job_out,
    normalize_code_or_422,
    require_service_token,
    select_metadata_providers,
)
from .matching import rank_candidates
from .media.artwork import ArtworkStore
from .media.nfo_import import read_movie_element, resolve_sidecar_nfo
from .normalize_code import is_format_equivalent
from .providers.base import ProviderSelection
from .services.catalog_classifications import validate_selection, work_classifications
from .services.offline_recovery import load_recovery
from .services.pan_offline_enqueue import OfflineEnqueueError, enqueue_work_offline
from .services.translation import WorkTranslator
from .services.work_samples import acquire_work_artwork
from .tag_models import fold_tag
from .tags import current_registry

if TYPE_CHECKING:
    from .api import Runtime


class OperationInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkTarget(OperationInput):
    work_id: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9:_-]+$")


class CatalogHealthInput(OperationInput):
    work_id: str | None = Field(default=None, max_length=240)
    limit: int = Field(default=48, ge=1, le=100)
    after: str = Field(default="", max_length=240)


class TagsEdit(WorkTarget):
    add: list[str] = Field(default_factory=list, max_length=200)
    remove: list[str] = Field(default_factory=list, max_length=200)


class FillInput(WorkTarget):
    fields: list[Literal["title", "plot", "actors", "artwork", "translation", "runtime_seconds"]] = Field(
        default=["actors", "artwork", "translation"], min_length=1, max_length=6
    )
    providers: ProviderSelection | None = None


class ClassificationFill(ClassificationSelection, WorkTarget):
    pass


class RecoverInput(OperationInput):
    task_id: str = Field(min_length=1, max_length=240, pattern=r"^[A-Za-z0-9:_-]+$")


class RecoverResultInput(RecoverInput):
    result_name: str | None = Field(default=None, min_length=1, max_length=512)
    info_hash: str | None = Field(default=None, pattern=r"^[0-9A-Fa-f]{40}$")

    @model_validator(mode="after")
    def validate_result(self) -> RecoverResultInput:
        if (self.result_name is None) != (self.info_hash is None):
            raise ValueError("result_name and verified info_hash are required together")
        name = self.result_name
        if name is not None and (
            name != name.strip() or name in {".", ".."}
            or any(c in "/\\" or ord(c) < 32 for c in name)
        ):
            raise ValueError("result_name must be an exact child name")
        return self


class DiscardPlaceholderInput(WorkTarget, RecoverInput):
    dry_run: bool = True


class LinkInput(WorkTarget):
    url: str = Field(min_length=3, max_length=4000, pattern=r"^(magnet:\?|https?://|ed2k://)")
    directory_id: OfflineDirectory | None = None


class IntakeItem(OperationInput):
    code: str | None = Field(default=None, min_length=2, max_length=100)
    source_url: str | None = Field(default=None, max_length=2000)
    external_ids: dict[str, str] = Field(default_factory=dict)
    magnet: str | None = Field(default=None, min_length=20, max_length=4000)
    title: str | None = Field(default=None, min_length=1, max_length=2000)
    tags: list[str] = Field(default_factory=list, max_length=200)
    download: bool = False
    directory_id: OfflineDirectory | None = None
    providers: ProviderSelection | None = None

    @model_validator(mode="after")
    def identity(self) -> IntakeItem:
        if self.magnet:
            if not self.download or self.source_url or self.external_ids:
                raise ValueError("magnet requires download=true and cannot combine with URL/external IDs")
        elif sum(bool(value) for value in (self.code, self.source_url, self.external_ids)) != 1:
            raise ValueError("provide exactly one identity")
        if self.download and not (self.code or self.magnet):
            raise ValueError("download needs code or magnet; source URLs only create metadata")
        if self.directory_id is not None and not self.download:
            raise ValueError("directory_id requires download=true")
        return self


def _missing_metadata_fields(work: Work, fields: Set[str]) -> set[str]:
    return {
        key for key, value in {
            "title": work.title, "plot": work.plot, "actors": work.actors,
            "artwork": work.artwork, "runtime_seconds": work.runtime_seconds,
        }.items() if key in fields and not value
    }


def apply_tags(repo: Repository, work_id: str, add: list[str], remove: list[str]) -> list[str]:
    """Resolve aliases and preserve tags outside the explicitly requested delta."""
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(404, "work not found")

    def identity(value: str) -> str:
        tag = current_registry().resolve(value)
        return tag.id if tag is not None else "source:" + fold_tag(value)

    removed = {identity(value) for value in remove}
    tags = [value for value in (work.tags or []) if identity(value) not in removed]
    present = {identity(value) for value in tags}
    for value in add:
        key = identity(value)
        if key not in present and value.strip():
            tag = current_registry().resolve(value)
            tags.append(tag.name if tag is not None else value.strip())
            present.add(key)
    if tags != work.tags:
        repo.update_work_fields(work, tags=tags)
    return tags


def build_operator_router(runtime: Callable[[Request], Runtime]) -> APIRouter:
    """Expose domain operations through the authenticated skill allowlist only."""
    router = APIRouter(prefix="/api/operator/v1", dependencies=[Depends(require_service_token)])

    @router.post("/catalog-health")
    def catalog_health(payload: CatalogHealthInput, request: Request) -> dict[str, object]:
        app_runtime = runtime(request)
        conditions = gap_conditions()
        with app_runtime.database.session() as session:
            scope = [] if payload.work_id is None else [Work.id == payload.work_id]
            if payload.work_id is not None and session.get(Work, payload.work_id) is None:
                raise HTTPException(404, "work not found")
            counters = {
                key.value: session.scalar(select(func.count()).select_from(Work).where(*scope, condition)) or 0
                for key, condition in conditions.items()
            }
            matches = session.execute(select(Work, *conditions.values()).where(
                *scope, Work.id > payload.after,
            ).order_by(Work.id).limit(payload.limit + 1)).all()
            page = matches[:payload.limit]
            ids = [row[0].id for row in page]
            assets = [(asset.id, asset.work_id, asset.path) for asset in session.scalars(
                select(MediaAsset).where(MediaAsset.work_id.in_(ids))
            ).all()]
            tasks = [
                {"task_id": task.id, "work_id": task.work_id, "status": task.status}
                for task in session.scalars(select(PanOfflineTask).where(
                    PanOfflineTask.work_id.in_(ids), PanOfflineTask.status == "done", PanOfflineTask.strm_path.is_(None),
                )).all()
            ]
        malformed = []
        for asset_id, work_id, location in assets:
            try:
                sidecar = resolve_sidecar_nfo(Path(location))
                if sidecar is not None:
                    read_movie_element(sidecar)
            except (OSError, ValueError):
                malformed.append({"asset_id": asset_id, "work_id": work_id})
        return {
            "missing": counters,
            "items": [
                {
                    "work_id": row[0].id,
                    "missing": [key.value for key, missed in zip(conditions, row[1:], strict=True) if missed],
                }
                for row in page
            ],
            "nfo_unreadable": malformed,
            "offline_without_export": tasks,
            "providers": app_runtime.providers.health_snapshot(),
            "next": page[-1][0].id if len(matches) > payload.limit else None,
        }

    @router.post("/discard-placeholder")
    def discard_placeholder(payload: DiscardPlaceholderInput, request: Request) -> dict[str, str]:
        app_runtime = runtime(request)
        result = {"work_id": payload.work_id, "task_id": payload.task_id}
        with app_runtime.database.session() as session:
            if not payload.dry_run:
                # Protect the eligibility check and delete from concurrent poller/catalog writes.
                if session.bind is not None and session.bind.dialect.name == "sqlite":
                    session.connection().exec_driver_sql("BEGIN IMMEDIATE")
                else:
                    session.scalar(select(Work).where(Work.id == payload.work_id).with_for_update())
                    session.scalar(
                        select(PanOfflineTask).where(PanOfflineTask.id == payload.task_id).with_for_update()
                    )
            repo = Repository(session)
            work = repo.get_work(payload.work_id)
            task = repo.get_pan_offline_task(payload.task_id)
            if work is None and task is None:
                return {**result, "outcome": "absent"}
            if work is None or task is None or task.work_id != work.id:
                raise HTTPException(409, "work and offline task do not match")
            if (task.status not in {"done", "failed", "cancelled"}
                or any((task.file_id, task.remote_path, task.remote_name, task.strm_path,
                        load_recovery(task).verified_result_name))
                or (task.recovery_json or {}).get("action")
                or [row.id for row in repo.list_pan_offline_tasks(work_id=work.id, limit=2)] != [task.id]):
                raise HTTPException(409, "offline task can still own results or active work")
            identities = {(row.provider, row.value.upper()) for row in repo.identities_for_work(work.id)}
            magnets = repo.list_work_magnets(work.id)
            digest = task.info_hash.upper()
            metadata = (work.plot, work.original_plot, work.original_title, work.studio, work.label,
                        work.series, work.artwork, work.runtime_seconds, work.release_date,
                        work.rating_value, work.reviews)
            if (not digest or identities != {(MAGNET_INGEST_PROVIDER, digest), ("btih", digest)}
                or len(magnets) != 1 or magnets[0].provider != MAGNET_INGEST_PROVIDER
                or magnets[0].info_hash.upper() != digest
                or work.family != "unknown" or work.primary_code or work.field_locks
                or set(work.tags or []) != set(MAGNET_INGEST_TAGS)
                or set((work.field_sources or {}).values()) != {MAGNET_INGEST_PROVIDER}
                or any(metadata)):
                raise HTTPException(409, "work has catalog data beyond the original magnet placeholder")
            if (work.id in app_runtime.library_prefs_store.load().want_list
                or repo.list_assets_for_work(work.id) or repo.collections_for_work(work.id)
                or session.scalar(select(exists().where(
                    SourceSnapshot.work_id == work.id, SourceSnapshot.provider != MAGNET_INGEST_PROVIDER
                )))
                or any(session.scalar(select(exists().where(model.work_id == work.id))) for model in (
                    MediaEmbyBinding, WorkClassification, WorkTaxonomy, CatalogPersonCredit,
                    CatalogWorkOrganization, CatalogWorkSeries,
                ))):
                raise HTTPException(409, "work has linked catalog or media data")
            if payload.dry_run:
                return {**result, "outcome": "planned"}
            # Older SQLite installations do not enforce cascades on these tables.
            for model in (PanOfflineTask, WorkMagnet, ExternalIdentity, SourceSnapshot, WorkActor):
                session.execute(delete(model).where(model.work_id == work.id))
            repo.delete_work(work.id)
        app_runtime.response_cache.clear()
        return {**result, "outcome": "removed"}

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
                                CatalogClassificationTerm.id.not_in(DURATION_FORM_IDS),
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
        selected = select_metadata_providers(app_runtime.providers, payload.providers)
        changed: list[str] = []
        errors: list[str] = []
        with app_runtime.database.session() as session:
            repo = Repository(session)
            work = repo.get_work(payload.work_id)
            if work is None:
                raise HTTPException(404, "work not found")
            fields = set(payload.fields) - set(work.field_locks or [])
            missing = _missing_metadata_fields(work, fields)
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
                        repo.commit()
                        batch = await app_runtime.providers.search(hints, provider_ids=selected)
                        session.expire_all()
                        work = repo.get_work(payload.work_id)
                        if work is None:
                            raise HTTPException(404, "work not found")
                        if not work.primary_code or not is_format_equivalent(code, work.primary_code):
                            raise HTTPException(409, "work identity changed during lookup")
                        fields = set(payload.fields) - set(work.field_locks or [])
                        missing = _missing_metadata_fields(work, fields)
                        for candidate in rank_candidates(hints, batch.records):
                            if not missing or candidate.decision is not MatchDecision.ACCEPT:
                                break
                            record = candidate.record
                            if not repo.can_merge_provider_record(work, record):
                                continue
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
                                runtime_seconds=(record.runtime_seconds
                                                 if "runtime_seconds" in missing and record.runtime_seconds
                                                 else None),
                                only_missing=True,
                                lock_edited=False,
                            )
                            written = list(updates)
                            if "actors" in missing and record.actors:
                                written.append("actors")
                            if "runtime_seconds" in missing and record.runtime_seconds:
                                written.append("runtime_seconds")
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
                _, result = await acquire_work_artwork(repo, ArtworkStore(
                    app_runtime.settings.data_dir / "artwork",
                    app_runtime.http,
                    max_bytes=app_runtime.settings.artwork_max_bytes,
                ), work)
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
    def recover(payload: RecoverResultInput, request: Request) -> dict[str, object]:
        # Re-adopt an existing remote result. Never submit a second remote download here.
        with runtime(request).database.session() as session:
            if (
                payload.result_name is not None and session.bind is not None
                and session.bind.dialect.name == "sqlite"
            ):
                session.connection().exec_driver_sql("BEGIN IMMEDIATE")
            repo = Repository(session)
            task = repo.get_pan_offline_task(payload.task_id)
            if task is None:
                raise HTTPException(404, "offline task not found")
            if task.status == "cancelled":
                raise HTTPException(409, "cancelled task needs an explicit new intake")
            state = load_recovery(task)
            if payload.result_name is not None:
                if task.backend != "openlist" or task.info_hash.upper() != (payload.info_hash or "").upper():
                    raise HTTPException(409, "verified result does not belong to this OpenList magnet task")
                if state.action:
                    raise HTTPException(409, "offline task has another action in progress")
                path = str(PurePosixPath(task.directory_id) / payload.result_name)
                if task.remote_path not in (None, path) or state.verified_result_name not in (
                    None, payload.result_name,
                ):
                    raise HTTPException(409, "offline task already has a different result")
                same_hint = PanOfflineTask.recovery_json["verified_result_name"].as_string()
                other = session.scalar(select(PanOfflineTask.id).where(
                    PanOfflineTask.id != task.id,
                    or_(
                        PanOfflineTask.remote_path == path,
                        and_(
                            PanOfflineTask.directory_id == task.directory_id,
                            same_hint == payload.result_name,
                        ),
                    ),
                ).limit(1))
                if other is not None:
                    raise HTTPException(409, "result already belongs to another offline task")
                state.verified_result_name = payload.result_name
            if task.strm_path and Path(task.strm_path).is_file():
                return {"outcome": "skipped", "job": job_out(task, repo.get_work(task.work_id)).model_dump()}
            if payload.result_name is None:
                repo.update_pan_offline_task(task, status="running", error=None)
            else:
                repo.update_pan_offline_task(
                    task, status="running", error=None, recovery_json=state.to_dict(),
                )
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
                    repo, app_runtime.pan_service, payload.work_id,
                    url=payload.url, directory_id=payload.directory_id,
                )
            except OfflineEnqueueError as exc:
                raise HTTPException(exc.status_code, exc.message) from exc
            return {
                "outcome": result.action,
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
                            magnet=payload.magnet, code=payload.code, title=payload.title,
                            dry_run=False, directory_id=payload.directory_id, providers=payload.providers,
                        ),
                        request,
                        repo,
                    )
                else:
                    result = await intake_offline_add(
                        OfflineAddRequest(
                            code=payload.code or "", dry_run=False,
                            directory_id=payload.directory_id, providers=payload.providers,
                        ),
                        request, repo,
                    )
                if result.work_id:
                    apply_tags(repo, result.work_id, payload.tags, [])
                    work = repo.get_work(result.work_id)
                    if work is not None and payload.title and payload.title != work.title:
                        repo.update_work_fields(work, title=payload.title)
                app_runtime.response_cache.clear()
                return {"outcome": result.action, **result.model_dump(mode="json")}
            selected = select_metadata_providers(app_runtime.providers, payload.providers)
            if payload.code:
                code = normalize_code_or_422(payload.code)
                _, family = extract_code(code)
                work = repo.find_work_by_code(code)
                hints = IdentityHints(
                    term=code, code=code, mode=QueryMode.CODE,
                    family=family, category=category_for_family(family),
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
            source = None
            if work is None:
                repo.commit()
                batch = await app_runtime.providers.search(hints, provider_ids=selected)
                record = next(
                    (item.record for item in rank_candidates(hints, batch.records)
                     if item.decision is MatchDecision.ACCEPT),
                    None,
                )
                if record is None:
                    return {
                        "outcome": "failed", "reason": "no accepted metadata",
                        "failures": [item.model_dump(mode="json") for item in batch.failures],
                    }
                existing = repo.find_work_by_provider_identity(record.provider, record.external_id)
                if existing is None and record.code:
                    existing = repo.find_work_by_code(record.code)
                created = existing is None
                work = repo.upsert_provider_record(record, overwrite=False, ranked_cast=True)
                source = record.provider
            apply_tags(repo, work.id, payload.tags, [])
            if payload.title and payload.title != work.title:
                repo.update_work_fields(work, title=payload.title)
            app_runtime.response_cache.clear()
            return {
                "outcome": "created" if created else "skipped",
                "work_id": work.id,
                "code": work.primary_code,
                "title": work.title,
                "selected_provider": source,
            }

    return router
