import hashlib
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from . import __version__
from .api_models import (
    AssetOut,
    BatchApplyOut,
    BatchPlanOut,
    CandidateOut,
    FilterWordsPayload,
    HealthOut,
    IdentifyOut,
    IdentifyRequest,
    IdentityOut,
    LibraryCreate,
    LibraryOut,
    ManualCandidateRequest,
    OrganizeApplyRequest,
    OrganizeRequest,
    PlanOut,
    ProviderDiagnoseOut,
    ProviderDiagnoseRequest,
    ProviderDiagnostic,
    ProviderListOut,
    ScanOut,
    TaskRunOut,
    WorkLookupOut,
    WorkLookupRequest,
    WorkOut,
)
from .config import Settings
from .db.models import Library, MatchCandidateRow, MediaAsset, Work
from .db.repository import Database, Repository
from .domain import IdentityHints, MatchEvidence, OperationPlan, ProviderRecord, ScoredCandidate
from .enums import ContentFamily, MatchDecision, MediaCategory, QueryMode
from .identity import IdentityAliasRules, extract_code, normalize_identity_value
from .matching import rank_candidates, score_candidate
from .media.artwork import ArtworkDownloadResult, ArtworkStore
from .media.nfo import build_nfo
from .media.organizer import Organizer
from .providers import JavBusProvider, JavDBProvider, JsonLdProvider, ProviderRegistry, ThePornDBProvider
from .services.alias_store import IdentityAliasStore
from .services.identify import IdentifyService
from .services.local_catalog import infer_media_category
from .services.path_filter import FilterWords, FilterWordsStore, MediaPathFilter
from .services.scanner import Scanner


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    database: Database
    http: httpx.AsyncClient
    providers: ProviderRegistry
    alias_store: IdentityAliasStore
    filter_words_store: FilterWordsStore


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    settings.ensure_directories()
    database = Database(settings.database_url)
    database.initialize()
    client = httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
        proxy=settings.proxy_url,
    )
    providers = ProviderRegistry(
        [
            JavDBProvider(client, settings.javdb_base_url, settings.request_retries),
            JavBusProvider(client, settings.javbus_base_url, settings.request_retries),
            ThePornDBProvider(client, settings.theporndb_graphql_url, settings.theporndb_token),
            JsonLdProvider(client, settings.request_retries),
        ]
    )
    alias_store = IdentityAliasStore(settings.data_dir / "identity-aliases.json")
    filter_words_store = FilterWordsStore(settings.data_dir / "filter-words.txt")
    app.state.runtime = Runtime(
        settings=settings,
        database=database,
        http=client,
        providers=providers,
        alias_store=alias_store,
        filter_words_store=filter_words_store,
    )
    try:
        yield
    finally:
        await client.aclose()


app = FastAPI(title="Shadow MDC", version=__version__, lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://127.0.0.1:5173", "http://localhost:5173"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


def runtime(request: Request) -> Runtime:
    value = request.app.state.runtime
    if not isinstance(value, Runtime):
        raise RuntimeError("application runtime is not initialized")
    return value


def repository(request: Request) -> Iterator[Repository]:
    with runtime(request).database.session() as session:
        yield Repository(session)


Repo = Annotated[Repository, Depends(repository)]


@app.get("/api/settings/identity-aliases", response_model=IdentityAliasRules)
def get_identity_aliases(request: Request) -> IdentityAliasRules:
    try:
        return runtime(request).alias_store.load()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc


@app.put("/api/settings/identity-aliases", response_model=IdentityAliasRules)
def update_identity_aliases(payload: IdentityAliasRules, request: Request) -> IdentityAliasRules:
    try:
        runtime(request).alias_store.save(payload)
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"cannot save identity alias rules: {exc}") from exc
    return payload


@app.get("/api/settings/filter-words", response_model=FilterWordsPayload)
def get_filter_words(request: Request) -> FilterWordsPayload:
    try:
        rules = runtime(request).filter_words_store.load()
    except ValueError as exc:
        raise HTTPException(status_code=500, detail=str(exc)) from exc
    return FilterWordsPayload(words=rules.words)


@app.put("/api/settings/filter-words", response_model=FilterWordsPayload)
def update_filter_words(payload: FilterWordsPayload, request: Request) -> FilterWordsPayload:
    try:
        saved = runtime(request).filter_words_store.save(FilterWords(words=payload.words))
    except OSError as exc:
        raise HTTPException(status_code=409, detail=f"cannot save filter words: {exc}") from exc
    return FilterWordsPayload(words=saved.words)


@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(version=__version__)


@app.get("/api/providers", response_model=ProviderListOut)
def providers(request: Request) -> ProviderListOut:
    return ProviderListOut(providers=runtime(request).providers.descriptors())


@app.get("/api/tasks", response_model=list[TaskRunOut])
def list_task_runs(repo: Repo, limit: int = 100) -> list[TaskRunOut]:
    safe_limit = min(max(limit, 1), 500)
    return [TaskRunOut.model_validate(item) for item in repo.list_task_runs(limit=safe_limit)]


@app.post("/api/providers/diagnose", response_model=ProviderDiagnoseOut)
async def diagnose_providers(payload: ProviderDiagnoseRequest, request: Request) -> ProviderDiagnoseOut:
    app_runtime = runtime(request)
    code = payload.code.strip().upper()
    hints = IdentityHints(
        term=code,
        mode=QueryMode.CODE,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        code=code,
    )
    batch = await app_runtime.providers.search(hints)
    record_counts: dict[str, int] = {}
    accepted_counts: dict[str, int] = {}
    for record in batch.records:
        record_counts[record.provider] = record_counts.get(record.provider, 0) + 1
        if score_candidate(hints, record).decision is MatchDecision.ACCEPT:
            accepted_counts[record.provider] = accepted_counts.get(record.provider, 0) + 1
    failures = {failure.provider: failure for failure in batch.failures}
    diagnostics: list[ProviderDiagnostic] = []
    for descriptor in app_runtime.providers.descriptors():
        failure = failures.get(descriptor.id)
        count = record_counts.get(descriptor.id, 0)
        accepted = accepted_counts.get(descriptor.id, 0)
        if not descriptor.configured:
            diagnostic = ProviderDiagnostic(provider=descriptor.id, status="not_configured")
        elif QueryMode.CODE not in descriptor.query_modes:
            diagnostic = ProviderDiagnostic(provider=descriptor.id, status="not_applicable")
        elif failure is not None:
            diagnostic = ProviderDiagnostic(
                provider=descriptor.id,
                status="failed",
                reason=failure.reason,
                detail=failure.detail,
            )
        elif accepted:
            diagnostic = ProviderDiagnostic(
                provider=descriptor.id,
                status="success",
                records=count,
                accepted=accepted,
            )
        elif count:
            diagnostic = ProviderDiagnostic(
                provider=descriptor.id,
                status="candidates",
                records=count,
            )
        else:
            diagnostic = ProviderDiagnostic(provider=descriptor.id, status="no_result")
        diagnostics.append(diagnostic)
    return ProviderDiagnoseOut(
        code=code,
        proxy_configured=bool(app_runtime.settings.proxy_url),
        retries=app_runtime.settings.request_retries,
        diagnostics=tuple(diagnostics),
    )


@app.post("/api/libraries", response_model=LibraryOut, status_code=status.HTTP_201_CREATED)
def create_library(payload: LibraryCreate, repo: Repo) -> LibraryOut:
    try:
        root = Path(payload.root_path).resolve()
    except OSError as exc:
        raise HTTPException(status_code=422, detail=f"root_path is unavailable: {exc}") from exc
    if not root.is_dir():
        raise HTTPException(status_code=422, detail="root_path must be an existing directory")
    try:
        library = repo.create_library(
            name=payload.name,
            root_path=str(root),
            category=payload.category or infer_media_category(payload.name, root.name),
            recursive=payload.recursive,
            organize_template=payload.organize_template,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="library name or root already exists") from exc
    return LibraryOut.model_validate(library)


@app.get("/api/libraries", response_model=list[LibraryOut])
def list_libraries(repo: Repo) -> list[LibraryOut]:
    return [LibraryOut.model_validate(item) for item in repo.list_libraries()]


@app.post("/api/libraries/{library_id}/scan", response_model=ScanOut)
def scan_library(library_id: str, request: Request, repo: Repo) -> ScanOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    task = repo.create_task_run(kind="scan", scope=library.root_path)
    try:
        app_runtime = runtime(request)
        result = Scanner(
            repo,
            app_runtime.alias_store.load(),
            MediaPathFilter(app_runtime.filter_words_store.load().words),
        ).scan(library)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo.finish_task_run(
        task,
        status="partial" if result.errors else "succeeded",
        summary={
            "discovered": result.discovered,
            "updated": result.updated,
            "cataloged": result.cataloged,
            "filtered": result.filtered,
            "skipped": result.skipped,
            "errors": len(result.errors),
        },
    )
    return ScanOut.model_validate(result.model_dump())


@app.get("/api/assets", response_model=list[AssetOut])
def list_assets(repo: Repo, state: str | None = None) -> list[AssetOut]:
    return [AssetOut.model_validate(item) for item in repo.list_assets(state=state)]


@app.get("/api/assets/{asset_id}", response_model=AssetOut)
def get_asset(asset_id: str, repo: Repo) -> AssetOut:
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return AssetOut.model_validate(asset)


@app.post("/api/assets/{asset_id}/identify", response_model=IdentifyOut)
async def identify_asset(
    asset_id: str,
    request: Request,
    repo: Repo,
    payload: IdentifyRequest | None = None,
) -> IdentifyOut:
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    if payload is not None and (payload.source_url or payload.title or payload.external_ids):
        hints = _override_hints(IdentityHints.model_validate(asset.hints), payload)
        repo.update_asset_hints(asset, hints)
    try:
        result = await IdentifyService(repo, runtime(request).providers).identify(asset_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return IdentifyOut.model_validate(result.model_dump())


@app.get("/api/assets/{asset_id}/candidates", response_model=list[CandidateOut])
def list_candidates(asset_id: str, repo: Repo) -> list[CandidateOut]:
    if repo.get_asset(asset_id) is None:
        raise HTTPException(status_code=404, detail="asset not found")
    return [_candidate_out(item) for item in repo.list_candidates(asset_id)]


@app.post(
    "/api/assets/{asset_id}/manual-candidate",
    response_model=CandidateOut,
    status_code=status.HTTP_201_CREATED,
)
def create_manual_candidate(
    asset_id: str,
    payload: ManualCandidateRequest,
    repo: Repo,
) -> CandidateOut:
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    hints = IdentityHints.model_validate(asset.hints)
    title = (payload.title or hints.title or hints.term).strip()
    if not title:
        raise HTTPException(status_code=422, detail="manual candidate requires a title")
    identity_payload = payload.model_dump_json(exclude_none=True)
    external_id = hashlib.sha256(f"{asset.id}:{identity_payload}".encode()).hexdigest()[:24]
    record = ProviderRecord(
        provider="local-manual",
        external_id=external_id,
        code=hints.code,
        title=title,
        family=hints.family,
        category=hints.category,
        studio=payload.studio or hints.studio,
        series=payload.series or hints.series,
        plot=payload.plot,
        actors=payload.actors or hints.actors,
        tags=payload.tags,
        fingerprints=hints.fingerprints,
        language="zh" if hints.family.value == "chinese" else None,
    )
    candidate = ScoredCandidate(
        record=record,
        score=1,
        decision=MatchDecision.REVIEW,
        evidence=(
            MatchEvidence(kind="manual", contribution=1, detail="user-created local candidate"),
        ),
    )
    row = repo.save_candidates(asset, [candidate])[0]
    return _candidate_out(row)


@app.post("/api/candidates/{candidate_id}/accept", response_model=WorkOut)
def accept_candidate(candidate_id: str, repo: Repo) -> WorkOut:
    try:
        work = repo.accept_candidate(candidate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return _work_out(repo, work)


@app.get("/api/works", response_model=list[WorkOut])
def list_works(repo: Repo) -> list[WorkOut]:
    return [_work_out(repo, work) for work in repo.list_works()]


@app.post("/api/works/lookup", response_model=WorkLookupOut)
async def lookup_work_by_code(
    payload: WorkLookupRequest,
    request: Request,
    repo: Repo,
) -> WorkLookupOut:
    code, family = extract_code(payload.code, MediaCategory.JAPAN)
    if code is None:
        raise HTTPException(status_code=422, detail="code is not a supported media identity")
    task = repo.create_task_run(kind="lookup", scope=code)
    hints = IdentityHints(
        term=code,
        mode=QueryMode.CODE,
        family=family,
        category={
            ContentFamily.JAV: MediaCategory.JAPAN,
            ContentFamily.CHINESE: MediaCategory.CHINA,
            ContentFamily.KOREAN: MediaCategory.KOREA,
            ContentFamily.WESTERN: MediaCategory.EUROPE,
        }.get(family, MediaCategory.OTHER),
        code=code,
    )
    batch = await runtime(request).providers.search(hints)
    ranked = rank_candidates(hints, list(batch.records))
    accepted = [item for item in ranked if item.decision is MatchDecision.ACCEPT]
    if not accepted:
        repo.finish_task_run(
            task,
            status="partial" if batch.records or batch.failures else "succeeded",
            summary={
                "records": len(batch.records),
                "accepted": 0,
                "failures": len(batch.failures),
            },
        )
        return WorkLookupOut(work=None, matched_records=len(batch.records), failures=batch.failures)
    primary = accepted[0].record
    work = repo.upsert_provider_record(primary, overwrite=True)
    for candidate in accepted[1:]:
        record = candidate.record
        if record.code and primary.code and normalize_identity_value(
            record.code
        ) == normalize_identity_value(primary.code):
            work = repo.upsert_provider_record(record, overwrite=False)
    repo.finish_task_run(
        task,
        status="partial" if batch.failures else "succeeded",
        summary={
            "records": len(batch.records),
            "accepted": len(accepted),
            "failures": len(batch.failures),
            "work_id": work.id,
        },
    )
    return WorkLookupOut(
        work=_work_out(repo, work),
        matched_records=len(accepted),
        failures=batch.failures,
    )


@app.get("/api/works/{work_id}", response_model=WorkOut)
def get_work(work_id: str, repo: Repo) -> WorkOut:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    return _work_out(repo, work)


@app.post("/api/works/{work_id}/refresh", response_model=IdentifyOut)
async def refresh_work_metadata(work_id: str, request: Request, repo: Repo) -> IdentifyOut:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    asset = repo.first_asset_for_work(work.id)
    if asset is None:
        raise HTTPException(status_code=409, detail="work has no media asset")
    result = await IdentifyService(repo, runtime(request).providers).identify(asset.id)
    return IdentifyOut.model_validate(result.model_dump())


@app.get("/api/works/{work_id}/nfo", response_class=PlainTextResponse)
def work_nfo(work_id: str, repo: Repo) -> Response:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    return PlainTextResponse(build_nfo(work, repo.identities_for_work(work.id)), media_type="application/xml")


@app.post("/api/works/{work_id}/artwork/download", response_model=ArtworkDownloadResult)
async def download_work_artwork(work_id: str, request: Request, repo: Repo) -> ArtworkDownloadResult:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    task = repo.create_task_run(kind="artwork", scope=work.primary_code or work.title)
    app_runtime = runtime(request)
    result, local_paths = await ArtworkStore(
        app_runtime.settings.data_dir / "artwork",
        app_runtime.http,
        max_bytes=app_runtime.settings.artwork_max_bytes,
    ).acquire(work)
    repo.update_artwork_local_paths(work, local_paths)
    repo.finish_task_run(
        task,
        status="partial" if result.failed else "succeeded",
        summary={
            "downloaded": result.downloaded,
            "cached": result.cached,
            "failed": result.failed,
        },
    )
    return result


@app.post("/api/assets/{asset_id}/organize/plan", response_model=PlanOut)
def organize_plan(asset_id: str, payload: OrganizeRequest, repo: Repo) -> PlanOut:
    asset, work, library = _organize_entities(repo, asset_id)
    try:
        plan = Organizer(repo).plan(
            asset=asset,
            work=work,
            library=library,
            mode=payload.mode,
            target_root=payload.target_root,
            template=payload.template,
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PlanOut.model_validate(plan.model_dump())


@app.post("/api/assets/{asset_id}/organize/apply", response_model=PlanOut)
def organize_apply(asset_id: str, payload: OrganizeApplyRequest, repo: Repo) -> PlanOut:
    asset, work, library = _organize_entities(repo, asset_id)
    try:
        plan = Organizer(repo).execute(
            asset=asset,
            work=work,
            library=library,
            identities=repo.identities_for_work(work.id),
            mode=payload.mode,
            token=payload.token,
            target_root=payload.target_root,
            template=payload.template,
            nfo_policy=payload.nfo_policy,
        )
    except (ValueError, FileExistsError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PlanOut.model_validate(plan.model_dump())


@app.post("/api/libraries/{library_id}/organize/plan", response_model=BatchPlanOut)
def organize_library_plan(library_id: str, payload: OrganizeRequest, repo: Repo) -> BatchPlanOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    try:
        plans = _library_plans(repo, library, payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _batch_plan_out(plans)


@app.post("/api/libraries/{library_id}/organize/apply", response_model=BatchApplyOut)
def organize_library_apply(library_id: str, payload: OrganizeApplyRequest, repo: Repo) -> BatchApplyOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    task = repo.create_task_run(kind=f"organize:{payload.mode.value}", scope=library.root_path)
    request_payload = OrganizeRequest(
        mode=payload.mode,
        target_root=payload.target_root,
        template=payload.template,
    )
    try:
        plans = _library_plans(repo, library, request_payload)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    current_token = _batch_token(plans)
    if current_token != payload.token:
        raise HTTPException(status_code=409, detail="batch plan changed; request a new preview")

    organizer = Organizer(repo)
    succeeded = 0
    errors: list[str] = []
    assets = repo.list_library_assets(library.id, identified_only=True)
    for asset in assets:
        if asset.work_id is None:
            continue
        work = repo.get_work(asset.work_id)
        if work is None:
            errors.append(f"{asset.path}: work not found")
            continue
        plan = next((item for item in plans if item.asset_id == asset.id), None)
        if plan is None:
            errors.append(f"{asset.path}: plan not found")
            continue
        try:
            organizer.execute(
                asset=asset,
                work=work,
                library=library,
                identities=repo.identities_for_work(work.id),
                mode=payload.mode,
                token=plan.token,
                target_root=payload.target_root,
                template=payload.template,
                nfo_policy=payload.nfo_policy,
            )
            succeeded += 1
        except (ValueError, FileExistsError, OSError) as exc:
            if len(errors) < 100:
                errors.append(f"{asset.path}: {exc}")
    attempted = len(plans)
    repo.finish_task_run(
        task,
        status="partial" if attempted != succeeded else "succeeded",
        summary={
            "attempted": attempted,
            "succeeded": succeeded,
            "failed": attempted - succeeded,
            "errors_recorded": len(errors),
        },
    )
    return BatchApplyOut(
        token=current_token,
        attempted=attempted,
        succeeded=succeeded,
        failed=attempted - succeeded,
        errors=tuple(errors),
    )


def _override_hints(current: IdentityHints, payload: IdentifyRequest) -> IdentityHints:
    updates: dict[str, object] = {}
    if payload.source_url:
        updates.update(term=payload.source_url, source_url=payload.source_url, mode=QueryMode.URL)
    if payload.external_ids:
        updates["external_ids"] = payload.external_ids
        if not payload.source_url:
            updates.update(
                term=next(iter(payload.external_ids.values())),
                mode=QueryMode.EXTERNAL_ID,
            )
    if payload.title:
        updates["title"] = payload.title
        if not payload.source_url and not payload.external_ids:
            updates.update(term=payload.title, mode=QueryMode.TEXT)
    return current.model_copy(update=updates)


def _candidate_out(row: MatchCandidateRow) -> CandidateOut:
    return CandidateOut(
        id=row.id,
        asset_id=row.asset_id,
        provider=row.provider,
        external_id=row.external_id,
        score=row.score,
        decision=row.decision,
        state=row.state,
        record=ProviderRecord.model_validate(row.record),
        evidence=row.evidence,
        created_at=row.created_at,
    )


def _work_out(repo: Repository, work: Work) -> WorkOut:
    identities = [
        IdentityOut(
            provider=item.provider,
            kind=item.kind,
            value=item.value,
            source_url=item.source_url,
        )
        for item in repo.identities_for_work(work.id)
    ]
    return WorkOut(
        id=work.id,
        title=work.title,
        original_title=work.original_title,
        primary_code=work.primary_code,
        family=work.family,
        category=MediaCategory(work.category),
        release_date=work.release_date,
        runtime_seconds=work.runtime_seconds,
        studio=work.studio,
        label=work.label,
        series=work.series,
        plot=work.plot,
        actors=work.actors,
        directors=work.directors,
        tags=work.tags,
        artwork=work.artwork,
        field_sources=work.field_sources,
        identities=identities,
        created_at=work.created_at,
        updated_at=work.updated_at,
    )


def _organize_entities(repo: Repository, asset_id: str) -> tuple[MediaAsset, Work, Library]:
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    if asset.work_id is None:
        raise HTTPException(status_code=409, detail="asset is not identified")
    work = repo.get_work(asset.work_id)
    library = repo.get_library(asset.library_id)
    if work is None or library is None:
        raise HTTPException(status_code=409, detail="asset references missing work or library")
    return asset, work, library


def _library_plans(
    repo: Repository,
    library: Library,
    payload: OrganizeRequest,
) -> list[OperationPlan]:
    organizer = Organizer(repo)
    plans: list[OperationPlan] = []
    for asset in repo.list_library_assets(library.id, identified_only=True):
        if asset.work_id is None:
            continue
        work = repo.get_work(asset.work_id)
        if work is None:
            continue
        plans.append(
            organizer.plan(
                asset=asset,
                work=work,
                library=library,
                mode=payload.mode,
                target_root=payload.target_root,
                template=payload.template,
            )
        )
    return plans


def _batch_token(plans: list[OperationPlan]) -> str:
    encoded = "\n".join(f"{plan.asset_id}:{plan.token}" for plan in plans).encode()
    return hashlib.sha256(encoded).hexdigest()


def _batch_plan_out(plans: list[OperationPlan]) -> BatchPlanOut:
    operation_count = sum(len(plan.operations) for plan in plans)
    conflict_count = sum(
        1 for plan in plans for operation in plan.operations if operation.conflict
    )
    sample_limit = 50
    return BatchPlanOut(
        token=_batch_token(plans),
        asset_count=len(plans),
        operation_count=operation_count,
        conflict_count=conflict_count,
        samples=tuple(plans[:sample_limit]),
        truncated=len(plans) > sample_limit,
    )


source_web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
packaged_web_dist = Path(__file__).resolve().parent / "static"
web_dist = source_web_dist if source_web_dist.is_dir() else packaged_web_dist
if web_dist.is_dir():
    app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
