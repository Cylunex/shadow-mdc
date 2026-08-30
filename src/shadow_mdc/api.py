import asyncio
import hashlib
import mimetypes
import unicodedata
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

import httpx
from fastapi import Depends, FastAPI, HTTPException, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles
from sqlalchemy.exc import IntegrityError

from . import __version__
from .api_models import (
    ActorSummaryOut,
    AssetInboxHintsOut,
    AssetInboxMediaOut,
    AssetInboxOut,
    AssetOut,
    BatchApplyOut,
    BatchPlanOut,
    BulkIdentifyOut,
    BulkIdentifyRequest,
    BulkTranslateOut,
    BulkTranslateRequest,
    CandidateOut,
    DirectoryActorAssignOut,
    DirectoryActorAssignRequest,
    FilterWordsPayload,
    HealthOut,
    IdentifyOut,
    IdentifyRequest,
    IdentityOut,
    LibraryCreate,
    LibraryOut,
    LibraryUpdate,
    ManualCandidateRequest,
    NonJavActorEdit,
    NonJavActorOut,
    OrganizeApplyRequest,
    OrganizeRequest,
    PlanOut,
    ProviderDiagnoseOut,
    ProviderDiagnoseRequest,
    ProviderDiagnostic,
    ProviderListOut,
    ScanOut,
    ScreenshotGenerateOut,
    ScreenshotGenerateRequest,
    TaskRunOut,
    WorkLookupOut,
    WorkLookupRequest,
    WorkOut,
)
from .config import Settings
from .db.models import Library, MatchCandidateRow, MediaAsset, Work
from .db.repository import Database, Repository
from .domain import (
    FileOperation,
    IdentityHints,
    MatchEvidence,
    MediaTechnicalInfo,
    OperationPlan,
    ProviderRecord,
    ScoredCandidate,
)
from .enums import (
    ContentFamily,
    MatchDecision,
    MediaCategory,
    NfoPolicy,
    OutputMode,
    QueryMode,
    RecognitionScope,
)
from .identity import IdentityAliasRules, build_identity_hints, extract_code, normalize_identity_value
from .matching import normalize_title, rank_candidates, score_candidate
from .media.artwork import ArtworkDownloadResult, ArtworkStore
from .media.nfo import build_nfo
from .media.organizer import Organizer, plan_move_cleanup
from .media.screenshots import capture_screenshot
from .providers import (
    AirAvProvider,
    AvSoxProvider,
    FanzaProvider,
    Fc2ClubProvider,
    Fc2HubProvider,
    FreeJavBtProvider,
    Jav321Provider,
    JavBusProvider,
    JavDBProvider,
    JavLibraryProvider,
    JsonLdProvider,
    MgstageProvider,
    ProviderRegistry,
    R18DevProvider,
    ThePornDBProvider,
)
from .services.actor_catalog import (
    ActorCatalogStore,
    ActorProfile,
    enrich_actor_aliases,
    sync_actor_catalog_from_relations,
)
from .services.alias_store import IdentityAliasStore
from .services.directory_actor_rules import (
    DirectoryActorRule,
    DirectoryActorRuleStore,
)
from .services.identify import IdentifyService
from .services.local_catalog import (
    build_local_catalog_record,
    family_for_category,
    is_generic_file_name,
    local_context_names,
)
from .services.non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    NonJavActorProfile,
    build_non_jav_actor_profile,
    enrich_non_jav_actor_aliases,
)
from .services.path_filter import FilterWords, FilterWordsStore, MediaPathFilter
from .services.scanner import Scanner
from .services.translation import GoogleTitleTranslator, TranslationCache


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    database: Database
    http: httpx.AsyncClient
    providers: ProviderRegistry
    translator: GoogleTitleTranslator
    alias_store: IdentityAliasStore
    actor_store: ActorCatalogStore
    non_jav_actor_store: NonJavActorCatalogStore
    directory_actor_store: DirectoryActorRuleStore
    filter_words_store: FilterWordsStore


@dataclass
class PendingIdentityGroup:
    hints: IdentityHints
    asset_ids: list[str]
    local_fallback: bool


@dataclass(frozen=True)
class LibraryPlanItem:
    asset: MediaAsset
    work: Work
    media_suffix: str | None
    plan: OperationPlan
    extra_operations: tuple[FileOperation, ...] = ()


def _http_client(settings: Settings, *, max_connections: int) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=settings.request_timeout_seconds,
        follow_redirects=True,
        headers={"User-Agent": settings.user_agent},
        proxy=settings.proxy_url,
        limits=httpx.Limits(
            max_connections=max_connections,
            max_keepalive_connections=0,
        ),
    )


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings = Settings()
    settings.ensure_directories()
    database = Database(settings.database_url)
    database.initialize()
    alias_store = IdentityAliasStore(settings.data_dir / "identity-aliases.json")
    actor_store = ActorCatalogStore(settings.data_dir / "actor-catalog.json")
    non_jav_actor_store = NonJavActorCatalogStore(settings.data_dir / "non-jav-actors.json")
    directory_actor_store = DirectoryActorRuleStore(settings.data_dir / "directory-actors.json")
    _repair_legacy_local_catalog(
        database,
        enrich_non_jav_actor_aliases(alias_store.load(), non_jav_actor_store.load()),
    )
    _migrate_cached_artwork(database, settings)
    with database.session() as session:
        repo = Repository(session)
        repo.repair_jav_actor_sources()
        repo.sync_all_work_actors()
    client = _http_client(settings, max_connections=settings.translation_concurrency + 4)
    provider_clients = tuple(
        _http_client(settings, max_connections=settings.identify_concurrency + 2) for _ in range(14)
    )
    source_clients = iter(provider_clients)
    providers = ProviderRegistry(
        [
            R18DevProvider(next(source_clients), settings.r18dev_base_url, settings.request_retries),
            FanzaProvider(next(source_clients), settings.fanza_base_url, settings.request_retries),
            JavLibraryProvider(next(source_clients), settings.javlibrary_base_url, settings.request_retries),
            MgstageProvider(next(source_clients), settings.mgstage_base_url, settings.request_retries),
            Fc2ClubProvider(next(source_clients), settings.fc2club_base_url, settings.request_retries),
            Fc2HubProvider(next(source_clients), settings.fc2hub_base_url, settings.request_retries),
            AirAvProvider(next(source_clients), settings.airav_base_url, settings.request_retries),
            AvSoxProvider(next(source_clients), settings.avsox_base_url, settings.request_retries),
            FreeJavBtProvider(next(source_clients), settings.freejavbt_base_url, settings.request_retries),
            JavDBProvider(next(source_clients), settings.javdb_base_url, settings.request_retries),
            JavBusProvider(next(source_clients), settings.javbus_base_url, settings.request_retries),
            Jav321Provider(next(source_clients), settings.jav321_base_url, settings.request_retries),
            ThePornDBProvider(next(source_clients), settings.theporndb_graphql_url, settings.theporndb_token),
            JsonLdProvider(next(source_clients), settings.request_retries),
        ],
        max_concurrent_calls=settings.provider_concurrency,
    )
    translator = GoogleTitleTranslator(
        client,
        TranslationCache(settings.data_dir / "translations.db"),
        enabled=settings.translation_enabled,
        endpoint=settings.translation_endpoint,
        target_language=settings.translation_target_language,
    )
    filter_words_store = FilterWordsStore(settings.data_dir / "filter-words.txt")
    app.state.runtime = Runtime(
        settings=settings,
        database=database,
        http=client,
        providers=providers,
        translator=translator,
        alias_store=alias_store,
        actor_store=actor_store,
        non_jav_actor_store=non_jav_actor_store,
        directory_actor_store=directory_actor_store,
        filter_words_store=filter_words_store,
    )
    try:
        yield
    finally:
        await asyncio.gather(client.aclose(), *(source.aclose() for source in provider_clients))


def _repair_legacy_local_catalog(
    database: Database,
    alias_rules: IdentityAliasRules,
) -> None:
    with database.session() as session:
        repo = Repository(session)
        repair_assets: dict[str, MediaAsset] = {}
        for asset in repo.list_assets():
            stored_hints = IdentityHints.model_validate(asset.hints)
            work = repo.get_work(asset.work_id) if asset.work_id is not None else None
            legacy_code_less_jav = stored_hints.code is None and (
                stored_hints.family is ContentFamily.JAV
                or (work is not None and work.primary_code is None and work.family == ContentFamily.JAV.value)
            )
            if legacy_code_less_jav:
                repair_assets[asset.id] = asset
        for asset in repair_assets.values():
            library = repo.get_library(asset.library_id)
            if library is None:
                continue
            stored_hints = IdentityHints.model_validate(asset.hints)
            source_path = Path(stored_hints.file_path or asset.path)
            root = Path(library.root_path)
            hints = build_identity_hints(
                source_path,
                source_url=stored_hints.source_url,
                external_ids=stored_hints.external_ids,
                fingerprints=stored_hints.fingerprints,
                duration_seconds=stored_hints.duration_seconds,
                media_locator=stored_hints.media_locator,
                context_names=local_context_names(source_path, root),
                alias_rules=alias_rules,
                category=MediaCategory.OTHER,
            )
            was_identified = asset.work_id is not None
            repo.update_asset_hints(asset, hints)
            queued = repo.queue_local_candidate(
                asset,
                build_local_catalog_record(
                    library_id=library.id,
                    root=root,
                    path=source_path,
                    hints=hints,
                ),
            )
            if was_identified and queued:
                repo.accept_local_candidate(asset.id)


def _migrate_cached_artwork(database: Database, settings: Settings) -> None:
    with database.session() as session:
        repo = Repository(session)
        store = ArtworkStore(
            settings.data_dir / "artwork",
            None,
            max_bytes=settings.artwork_max_bytes,
        )
        for work in repo.list_works():
            local_paths = store.adopt_cached(work)
            if local_paths:
                repo.update_artwork_local_paths(work, local_paths)


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
            category=payload.category or MediaCategory.OTHER,
            recursive=payload.recursive,
            organize_template=payload.organize_template,
            recognition_scope=payload.recognition_scope,
        )
    except IntegrityError as exc:
        raise HTTPException(status_code=409, detail="library name or root already exists") from exc
    return LibraryOut.model_validate(library)


@app.get("/api/libraries", response_model=list[LibraryOut])
def list_libraries(repo: Repo) -> list[LibraryOut]:
    return [LibraryOut.model_validate(item) for item in repo.list_libraries()]


@app.patch("/api/libraries/{library_id}", response_model=LibraryOut)
def update_library(library_id: str, payload: LibraryUpdate, repo: Repo) -> LibraryOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    updated = repo.update_library_recognition_scope(library, payload.recognition_scope)
    return LibraryOut.model_validate(updated)


@app.get("/api/actors", response_model=tuple[ActorProfile, ...])
def list_actor_catalog(request: Request, repo: Repo) -> tuple[ActorProfile, ...]:
    app_runtime = runtime(request)
    return sync_actor_catalog_from_relations(
        app_runtime.actor_store,
        repo.list_actor_work_relations(),
        app_runtime.alias_store.load(),
    )


@app.get("/api/non-jav-actors", response_model=tuple[NonJavActorOut, ...])
def list_non_jav_actors(request: Request) -> tuple[NonJavActorOut, ...]:
    app_runtime = runtime(request)
    return tuple(_non_jav_actor_out(actor) for actor in app_runtime.non_jav_actor_store.load().actors)


@app.post("/api/non-jav-actors", response_model=NonJavActorOut, status_code=201)
def create_non_jav_actor(payload: NonJavActorEdit, request: Request) -> NonJavActorOut:
    app_runtime = runtime(request)
    if app_runtime.non_jav_actor_store.get(payload.name) is not None:
        raise HTTPException(status_code=409, detail="non-JAV actor already exists")
    actor = build_non_jav_actor_profile(**payload.model_dump())
    app_runtime.non_jav_actor_store.upsert(actor)
    return _non_jav_actor_out(actor)


@app.patch("/api/non-jav-actors/{actor_name}", response_model=NonJavActorOut)
def update_non_jav_actor(
    actor_name: str,
    payload: NonJavActorEdit,
    request: Request,
) -> NonJavActorOut:
    app_runtime = runtime(request)
    existing = app_runtime.non_jav_actor_store.get(actor_name)
    if existing is None:
        raise HTTPException(status_code=404, detail="non-JAV actor not found")
    actor = build_non_jav_actor_profile(
        **payload.model_dump(),
        image_file=existing.image_file,
    )
    app_runtime.non_jav_actor_store.upsert(actor, previous_name=actor_name)
    if (
        unicodedata.normalize("NFKC", actor_name).casefold()
        != unicodedata.normalize("NFKC", actor.name).casefold()
    ):
        app_runtime.directory_actor_store.rename_actor(actor_name, actor.name)
    return _non_jav_actor_out(actor)


@app.delete("/api/non-jav-actors/{actor_name}", status_code=204)
def delete_non_jav_actor(actor_name: str, request: Request) -> Response:
    try:
        runtime(request).non_jav_actor_store.delete(actor_name)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail="non-JAV actor not found") from exc
    return Response(status_code=204)


@app.post("/api/non-jav-actors/{actor_name}/image", response_model=NonJavActorOut)
async def upload_non_jav_actor_image(actor_name: str, request: Request) -> NonJavActorOut:
    app_runtime = runtime(request)
    actor = app_runtime.non_jav_actor_store.get(actor_name)
    if actor is None:
        raise HTTPException(status_code=404, detail="non-JAV actor not found")
    content = await request.body()
    if not content:
        raise HTTPException(status_code=422, detail="image is empty")
    if len(content) > app_runtime.settings.artwork_max_bytes:
        raise HTTPException(status_code=413, detail="image exceeds configured size limit")
    detected = _detect_image(content)
    if detected is None:
        raise HTTPException(status_code=415, detail="only JPEG, PNG, WebP and GIF images are supported")
    extension, _media_type = detected
    file_key = hashlib.sha256(
        unicodedata.normalize("NFKC", actor.name).casefold().encode("utf-8")
    ).hexdigest()
    filename = f"{file_key}{extension}"
    image_path = app_runtime.settings.data_dir / "actor-images" / filename
    temporary = image_path.with_suffix(f"{image_path.suffix}.tmp")
    temporary.write_bytes(content)
    temporary.replace(image_path)
    updated = actor.model_copy(update={"image_file": filename})
    app_runtime.non_jav_actor_store.upsert(updated)
    return _non_jav_actor_out(updated)


@app.get("/api/non-jav-actor-images/{filename}")
def non_jav_actor_image(filename: str, request: Request) -> Response:
    app_runtime = runtime(request)
    if Path(filename).name != filename:
        raise HTTPException(status_code=404, detail="image not found")
    referenced = {
        actor.image_file
        for actor in app_runtime.non_jav_actor_store.load().actors
        if actor.image_file is not None
    }
    if filename not in referenced:
        raise HTTPException(status_code=404, detail="image not found")
    image_path = app_runtime.settings.data_dir / "actor-images" / filename
    if not image_path.is_file():
        raise HTTPException(status_code=404, detail="image not found")
    return FileResponse(
        image_path,
        media_type=mimetypes.guess_type(filename)[0] or "application/octet-stream",
    )


@app.post(
    "/api/assets/{asset_id}/directory-actor",
    response_model=DirectoryActorAssignOut,
)
def assign_directory_actor(
    asset_id: str,
    payload: DirectoryActorAssignRequest,
    request: Request,
    repo: Repo,
) -> DirectoryActorAssignOut:
    asset = repo.get_asset(asset_id)
    if asset is None:
        raise HTTPException(status_code=404, detail="asset not found")
    library = repo.get_library(asset.library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    root = Path(library.root_path).resolve()
    asset_directory = Path(asset.path).resolve().parent
    directory = Path(payload.directory).resolve() if payload.directory else asset_directory
    try:
        directory.relative_to(root)
        asset_directory.relative_to(directory)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="selected directory must contain the asset and stay inside its library",
        ) from exc

    app_runtime = runtime(request)
    actor_name = payload.actor.strip()
    app_runtime.directory_actor_store.upsert(
        DirectoryActorRule(
            directory=str(directory),
            actor=actor_name,
            category=payload.category,
        ),
        replace_descendants=True,
    )
    existing_profile = app_runtime.non_jav_actor_store.get(actor_name)
    if existing_profile is None:
        existing_profile = build_non_jav_actor_profile(
            name=actor_name,
            aliases=(),
            groups=("user-directory",),
            categories=(payload.category,),
        )
        app_runtime.non_jav_actor_store.upsert(existing_profile)

    matched_assets = 0
    cataloged = 0
    skipped = 0
    for current in repo.list_assets():
        current_path = Path(current.path).resolve()
        if current.library_id != library.id or not current_path.is_relative_to(directory):
            continue
        matched_assets += 1
        hints = IdentityHints.model_validate(current.hints)
        if hints.code is not None:
            skipped += 1
            continue
        existing_work = repo.get_work(current.work_id) if current.work_id is not None else None
        if existing_work is not None and repo.work_has_verified_evidence(existing_work.id):
            skipped += 1
            continue
        updated_hints = hints.model_copy(
            update={
                "actors": (actor_name,),
                "category": payload.category,
                "family": family_for_category(payload.category),
                "alias_evidence": (*hints.alias_evidence, "directory-actor:confirmed"),
            }
        )
        repo.update_asset_hints(current, updated_hints)
        record = build_local_catalog_record(
            library_id=library.id,
            root=root,
            path=current_path,
            hints=updated_hints,
            actor_directory=directory,
        )
        repo.refresh_local_catalog_asset(current, record)
        cataloged += 1
    return DirectoryActorAssignOut(
        directory=str(directory),
        actor=actor_name,
        matched_assets=matched_assets,
        cataloged=cataloged,
        skipped=skipped,
    )


@app.post("/api/libraries/{library_id}/scan", response_model=ScanOut)
def scan_library(library_id: str, request: Request, repo: Repo) -> ScanOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    task = repo.create_task_run(kind="scan", scope=library.root_path)
    try:
        app_runtime = runtime(request)
        alias_rules = app_runtime.alias_store.load()
        actor_catalog = sync_actor_catalog_from_relations(
            app_runtime.actor_store,
            repo.list_actor_work_relations(),
            alias_rules,
        )
        effective_alias_rules = enrich_non_jav_actor_aliases(
            enrich_actor_aliases(alias_rules, actor_catalog),
            app_runtime.non_jav_actor_store.load(),
        )
        non_jav_actor_catalog = app_runtime.non_jav_actor_store.load()
        result = Scanner(
            repo,
            effective_alias_rules,
            MediaPathFilter(app_runtime.filter_words_store.load().words),
            app_runtime.directory_actor_store.load(),
            non_jav_actor_catalog,
        ).scan(library)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    repo.finish_task_run(
        task,
        status="partial" if result.errors else "succeeded",
        summary={
            "discovered": result.discovered,
            "updated": result.updated,
            "queued": result.queued,
            "identified": result.identified,
            "filtered": result.filtered,
            "skipped": result.skipped,
            "errors": len(result.errors),
        },
    )
    return ScanOut.model_validate(result.model_dump())


@app.post("/api/libraries/{library_id}/identify", response_model=BulkIdentifyOut)
async def identify_library(
    library_id: str,
    payload: BulkIdentifyRequest,
    request: Request,
    repo: Repo,
) -> BulkIdentifyOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    groups: dict[str, PendingIdentityGroup] = {}
    assets = sorted(
        repo.list_library_assets(library.id),
        key=lambda asset: (asset.updated_at, asset.path.casefold()),
    )
    scope_skipped = 0
    catalog_reused = 0
    for asset in assets:
        if asset.state == "identified":
            continue
        hints = IdentityHints.model_validate(asset.hints)
        if (
            library.recognition_scope == RecognitionScope.JAV_ONLY.value
            and hints.family is not ContentFamily.JAV
        ):
            scope_skipped += 1
            continue
        local_fallback = not hints.code
        if hints.code:
            existing = repo.find_work_by_code(hints.code)
            if existing is not None:
                repo.attach_asset_to_work(asset, existing)
                catalog_reused += 1
                continue
            key = f"code:{normalize_identity_value(hints.code)}"
            search_hints = hints
        else:
            local_record = repo.local_candidate_record(asset.id)
            if local_record is None:
                continue
            search_hints = hints.model_copy(
                update={
                    "term": local_record.title,
                    "mode": QueryMode.TEXT,
                    "title": local_record.title,
                    "studio": local_record.studio or hints.studio,
                    "series": local_record.series or hints.series,
                    "actors": local_record.actors or hints.actors,
                }
            )
            repo.update_asset_hints(asset, search_hints)
            normalized_title = normalize_title(local_record.title) or asset.id
            key = f"text:{search_hints.category.value}:{normalized_title}"
        if key in groups:
            groups[key].asset_ids.append(asset.id)
        else:
            groups[key] = PendingIdentityGroup(
                hints=search_hints,
                asset_ids=[asset.id],
                local_fallback=local_fallback,
            )
    selected = list(groups.values())[: payload.limit]
    app_runtime = runtime(request)
    service = IdentifyService(repo, app_runtime.providers)
    online_identified = 0
    local_optimized = 0
    failures = 0
    attempted = 0
    accepted_work_ids: set[str] = set()
    chunk_size = app_runtime.settings.identify_concurrency
    for start in range(0, len(selected), chunk_size):
        chunk = selected[start : start + chunk_size]
        batches = await asyncio.gather(*(app_runtime.providers.search(group.hints) for group in chunk))
        for group, batch in zip(chunk, batches, strict=True):
            failures += sum(failure.reason != "cooldown" for failure in batch.failures)
            for asset_id in group.asset_ids:
                attempted += 1
                result = service.apply_batch(asset_id, batch)
                if result.accepted_work_id is not None:
                    online_identified += 1
                    accepted_work_ids.add(result.accepted_work_id)
                elif group.local_fallback:
                    work = repo.accept_local_candidate(asset_id)
                    if work is not None:
                        local_optimized += 1
                        accepted_work_ids.add(work.id)
    translation_slots = asyncio.Semaphore(app_runtime.settings.translation_concurrency)

    async def translate(work_id: str) -> None:
        work = repo.get_work(work_id)
        if work is None:
            return
        async with translation_slots:
            await app_runtime.translator.translate_work(repo, work)

    await asyncio.gather(*(translate(work_id) for work_id in accepted_work_ids))
    remaining = max(0, len(groups) - len(selected))
    attempted += catalog_reused
    identified = online_identified + catalog_reused + local_optimized
    code_queries = sum(not group.local_fallback for group in selected)
    title_queries = len(selected) - code_queries
    task = repo.create_task_run(kind="identify:library", scope=library.root_path)
    repo.finish_task_run(
        task,
        status="succeeded" if identified == attempted else "partial",
        summary={
            "queried_identities": len(selected),
            "code_queries": code_queries,
            "title_queries": title_queries,
            "attempted_assets": attempted,
            "identified": identified,
            "online_identified": online_identified,
            "catalog_reused": catalog_reused,
            "local_optimized": local_optimized,
            "unresolved": attempted - identified,
            "provider_failures": failures,
            "remaining_identities": remaining,
            "scope_skipped": scope_skipped,
        },
    )
    return BulkIdentifyOut(
        queried_identities=len(selected),
        code_queries=code_queries,
        title_queries=title_queries,
        attempted_assets=attempted,
        identified=identified,
        online_identified=online_identified,
        catalog_reused=catalog_reused,
        local_optimized=local_optimized,
        unresolved=attempted - identified,
        provider_failures=failures,
        remaining_identities=remaining,
        scope_skipped=scope_skipped,
    )


@app.get("/api/assets", response_model=list[AssetOut])
def list_assets(repo: Repo, state: str | None = None) -> list[AssetOut]:
    return [AssetOut.model_validate(item) for item in repo.list_assets(state=state)]


@app.get("/api/inbox", response_model=list[AssetInboxOut])
def list_inbox_assets(repo: Repo) -> list[AssetInboxOut]:
    output: list[AssetInboxOut] = []
    for asset in repo.list_assets():
        if asset.state == "identified":
            continue
        hints = IdentityHints.model_validate(asset.hints)
        media_info = MediaTechnicalInfo.model_validate(asset.media_info or {})
        output.append(
            AssetInboxOut(
                id=asset.id,
                library_id=asset.library_id,
                path=asset.path,
                state=asset.state,
                hints=AssetInboxHintsOut(
                    family=hints.family.value,
                    category=hints.category,
                    code=hints.code,
                    title=hints.title,
                    media_locator=hints.media_locator,
                    studio=hints.studio,
                    series=hints.series,
                    actors=hints.actors,
                ),
                media_info=AssetInboxMediaOut(
                    video_codec=media_info.video_codec,
                    audio_codec=media_info.audio_codec,
                    hdr_format=media_info.hdr_format,
                    quality_label=media_info.quality_label,
                ),
            )
        )
    return output


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
        app_runtime = runtime(request)
        result = await IdentifyService(repo, app_runtime.providers).identify(asset_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    current_hints = IdentityHints.model_validate(asset.hints)
    if result.accepted_work_id is None and current_hints.code is None:
        local_work = repo.accept_local_candidate(asset.id)
        if local_work is not None:
            result = result.model_copy(update={"accepted_work_id": local_work.id})
    if result.accepted_work_id is not None:
        work = repo.get_work(result.accepted_work_id)
        if work is not None:
            await app_runtime.translator.translate_work(repo, work)
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
        evidence=(MatchEvidence(kind="manual", contribution=1, detail="user-created local candidate"),),
    )
    row = repo.save_candidates(asset, [candidate])[0]
    return _candidate_out(row)


@app.post("/api/candidates/{candidate_id}/accept", response_model=WorkOut)
async def accept_candidate(candidate_id: str, request: Request, repo: Repo) -> WorkOut:
    try:
        work = repo.accept_candidate(candidate_id)
    except LookupError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    app_runtime = runtime(request)
    await app_runtime.translator.translate_work(repo, work)
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
        if (
            record.code
            and primary.code
            and normalize_identity_value(record.code) == normalize_identity_value(primary.code)
        ):
            work = repo.upsert_provider_record(record, overwrite=False)
    app_runtime = runtime(request)
    await app_runtime.translator.translate_work(repo, work)
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
    app_runtime = runtime(request)
    result = await IdentifyService(repo, app_runtime.providers).identify(asset.id)
    accepted = repo.get_work(result.accepted_work_id) if result.accepted_work_id is not None else None
    if accepted is not None:
        await app_runtime.translator.translate_work(repo, accepted)
    return IdentifyOut.model_validate(result.model_dump())


@app.post("/api/works/translate", response_model=BulkTranslateOut)
async def translate_work_titles(
    payload: BulkTranslateRequest,
    request: Request,
    repo: Repo,
) -> BulkTranslateOut:
    works = repo.list_works()
    selected = works[: payload.limit]
    translated = 0
    skipped = 0
    failed = 0
    errors: list[str] = []
    translator = runtime(request).translator
    for work in selected:
        result = await translator.translate_work(repo, work)
        if result.status == "translated":
            translated += 1
        elif result.status == "skipped":
            skipped += 1
        else:
            failed += 1
            errors.append(f"{work.primary_code or work.id}: {result.detail or 'translation failed'}")
    task = repo.create_task_run(kind="translate:title", scope="works")
    repo.finish_task_run(
        task,
        status="partial" if failed else "succeeded",
        summary={
            "attempted": len(selected),
            "translated": translated,
            "skipped": skipped,
            "failed": failed,
            "remaining": max(0, len(works) - len(selected)),
        },
    )
    return BulkTranslateOut(
        attempted=len(selected),
        translated=translated,
        skipped=skipped,
        failed=failed,
        remaining=max(0, len(works) - len(selected)),
        errors=tuple(errors[:20]),
    )


@app.get("/api/works/{work_id}/nfo", response_class=PlainTextResponse)
def work_nfo(work_id: str, repo: Repo) -> Response:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    return PlainTextResponse(build_nfo(work, repo.identities_for_work(work.id)), media_type="application/xml")


@app.get("/api/works/{work_id}/artwork/{kind}", response_class=FileResponse)
def work_artwork_file(work_id: str, kind: str, request: Request, repo: Repo) -> Response:
    if kind not in {"poster", "fanart"}:
        raise HTTPException(status_code=404, detail="artwork kind not found")
    if repo.get_work(work_id) is None:
        raise HTTPException(status_code=404, detail="work not found")
    root = (runtime(request).settings.data_dir / "artwork" / work_id).resolve()
    path = next((item for item in sorted(root.glob(f"{kind}.*")) if item.is_file()), None)
    if path is None:
        raise HTTPException(status_code=404, detail="cached artwork not found")
    return FileResponse(path)


@app.post("/api/works/{work_id}/artwork/download", response_model=ArtworkDownloadResult)
async def download_work_artwork(work_id: str, request: Request, repo: Repo) -> ArtworkDownloadResult:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    app_runtime = runtime(request)
    result, local_paths = await ArtworkStore(
        app_runtime.settings.data_dir / "artwork",
        app_runtime.http,
        max_bytes=app_runtime.settings.artwork_max_bytes,
    ).acquire(work)
    # Keep network I/O outside the write transaction. A fresh, short transaction also
    # avoids SQLite read-to-write upgrade conflicts when several works are cached at once.
    with app_runtime.database.session() as write_session:
        write_repo = Repository(write_session)
        stored_work = write_repo.get_work(work_id)
        if stored_work is None:
            raise HTTPException(status_code=404, detail="work not found")
        task = write_repo.create_task_run(
            kind="artwork",
            scope=stored_work.primary_code or stored_work.title,
        )
        write_repo.update_artwork_local_paths(stored_work, local_paths)
        write_repo.finish_task_run(
            task,
            status="partial" if result.failed else "succeeded",
            summary={
                "downloaded": result.downloaded,
                "cached": result.cached,
                "failed": result.failed,
            },
        )
    return result


@app.post(
    "/api/libraries/{library_id}/screenshots",
    response_model=ScreenshotGenerateOut,
)
async def generate_library_screenshots(
    library_id: str,
    payload: ScreenshotGenerateRequest,
    request: Request,
    repo: Repo,
) -> ScreenshotGenerateOut:
    """Generate work artwork only for trusted, identified non-JAV local videos."""

    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    task = repo.create_task_run(kind="screenshots:non-jav", scope=library.root_path)
    generated = 0
    skipped_strm = 0
    skipped_cached = 0
    skipped_untrusted = 0
    failed = 0
    attempted = 0
    errors: list[str] = []
    library_root = Path(library.root_path).resolve()
    for asset in repo.list_library_assets(library.id, identified_only=True):
        if not Path(asset.path).resolve().is_relative_to(library_root):
            continue
        if asset.work_id is None:
            continue
        work = repo.get_work(asset.work_id)
        if work is None or work.family == ContentFamily.JAV.value:
            continue
        if not _strict_non_jav_organize_candidate(asset, work, require_screenshot=False):
            skipped_untrusted += 1
            continue
        if Path(asset.path).suffix.casefold() == ".strm":
            skipped_strm += 1
            continue
        if _asset_generated_artwork_ready(asset, work):
            skipped_cached += 1
            continue
        if attempted >= payload.limit:
            continue
        attempted += 1
        try:
            capture = await asyncio.to_thread(
                capture_screenshot,
                asset.path,
                runtime(request).settings.data_dir / "artwork" / work.id,
                duration_seconds=asset.duration_seconds,
            )
            repo.set_generated_artwork(
                work,
                asset_id=asset.id,
                fanart_path=str(capture.fanart),
                poster_path=str(capture.poster),
                timestamp_seconds=capture.timestamp_seconds,
            )
            generated += 1
        except (FileNotFoundError, OSError, RuntimeError, ValueError) as exc:
            failed += 1
            errors.append(f"{asset.path}: {exc}")
    repo.finish_task_run(
        task,
        status="partial" if failed else "succeeded",
        summary={
            "attempted": attempted,
            "generated": generated,
            "skipped_strm": skipped_strm,
            "skipped_cached": skipped_cached,
            "skipped_untrusted": skipped_untrusted,
            "failed": failed,
        },
    )
    return ScreenshotGenerateOut(
        attempted=attempted,
        generated=generated,
        skipped_strm=skipped_strm,
        skipped_cached=skipped_cached,
        skipped_untrusted=skipped_untrusted,
        failed=failed,
        errors=tuple(errors[:20]),
    )


@app.post("/api/assets/{asset_id}/organize/plan", response_model=PlanOut)
def organize_plan(
    asset_id: str,
    payload: OrganizeRequest,
    request: Request,
    repo: Repo,
) -> PlanOut:
    asset, work, library = _organize_entities(repo, asset_id)
    try:
        plan = _asset_organize_plan(
            repo,
            asset,
            work,
            library,
            payload,
            _media_path_filter(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return PlanOut.model_validate(plan.model_dump())


@app.post("/api/assets/{asset_id}/organize/apply", response_model=PlanOut)
def organize_apply(
    asset_id: str,
    payload: OrganizeApplyRequest,
    request: Request,
    repo: Repo,
) -> PlanOut:
    asset, work, library = _organize_entities(repo, asset_id)
    try:
        request_payload = OrganizeRequest(
            mode=payload.mode,
            target_root=payload.target_root,
            template=payload.template,
        )
        preview = _asset_organize_plan(
            repo,
            asset,
            work,
            library,
            request_payload,
            _media_path_filter(request),
        )
        cleanup_operations = tuple(
            operation
            for operation in preview.operations
            if operation.kind.value in {"delete_filtered_file", "remove_directory"}
        )
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
            extra_operations=cleanup_operations,
        )
    except (ValueError, FileExistsError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PlanOut.model_validate(plan.model_dump())


@app.post("/api/libraries/{library_id}/organize/plan", response_model=BatchPlanOut)
def organize_library_plan(
    library_id: str,
    payload: OrganizeRequest,
    request: Request,
    repo: Repo,
) -> BatchPlanOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    try:
        plans = _library_plans(repo, library, payload, _media_path_filter(request))
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    return _batch_plan_out(plans)


@app.post("/api/libraries/{library_id}/organize/apply", response_model=BatchApplyOut)
def organize_library_apply(
    library_id: str,
    payload: OrganizeApplyRequest,
    request: Request,
    repo: Repo,
) -> BatchApplyOut:
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
        plan_items = _library_plan_items(
            repo,
            library,
            request_payload,
            _media_path_filter(request),
        )
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    plans = [item.plan for item in plan_items]
    current_token = _batch_token(plans)
    if current_token != payload.token:
        raise HTTPException(status_code=409, detail="batch plan changed; request a new preview")

    organizer = Organizer(repo)
    succeeded = 0
    errors: list[str] = []
    completed_work_ids: set[str] = set()
    for item in plan_items:
        try:
            nfo_policy = (
                NfoPolicy.SKIP
                if item.work.id in completed_work_ids and payload.nfo_policy is NfoPolicy.ERROR
                else payload.nfo_policy
            )
            organizer.execute(
                asset=item.asset,
                work=item.work,
                library=library,
                identities=repo.identities_for_work(item.work.id),
                mode=payload.mode,
                token=item.plan.token,
                target_root=payload.target_root,
                template=payload.template,
                nfo_policy=nfo_policy,
                media_suffix=item.media_suffix,
                extra_operations=item.extra_operations,
            )
            succeeded += 1
            completed_work_ids.add(item.work.id)
        except (ValueError, FileExistsError, OSError) as exc:
            if len(errors) < 100:
                errors.append(f"{item.asset.path}: {exc}")
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


def _non_jav_actor_out(actor: NonJavActorProfile) -> NonJavActorOut:
    return NonJavActorOut(
        name=actor.name,
        aliases=actor.aliases,
        groups=actor.groups,
        categories=actor.categories,
        match_names=actor.match_names,
        image_url=(f"/api/non-jav-actor-images/{actor.image_file}" if actor.image_file is not None else None),
        biography=actor.biography,
        notes=actor.notes,
    )


def _detect_image(content: bytes) -> tuple[str, str] | None:
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg", "image/jpeg"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png", "image/png"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif", "image/gif"
    if len(content) >= 12 and content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp", "image/webp"
    return None


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
    actor_entities = [
        ActorSummaryOut(id=actor.id, name=actor.name, image_url=actor.image_url)
        for actor in repo.actors_for_work(work.id)
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
        actor_entities=actor_entities,
        directors=work.directors,
        tags=work.tags,
        artwork=work.artwork,
        image_url=_work_display_artwork(work, "poster"),
        fanart_url=_work_display_artwork(work, "fanart"),
        field_sources=work.field_sources,
        identities=identities,
        created_at=work.created_at,
        updated_at=work.updated_at,
    )


def _work_display_artwork(work: Work, kind: str) -> str | None:
    is_fanart = kind == "fanart"
    matching = [
        item
        for item in work.artwork
        if (str(item.get("kind", "thumb")).casefold() in {"fanart", "background", "backdrop"}) is is_fanart
    ]
    if any(isinstance((path := item.get("local_path")), str) and Path(path).is_file() for item in matching):
        return f"/api/works/{work.id}/artwork/{kind}"
    return next(
        (
            url
            for item in matching
            if isinstance((url := item.get("url")), str) and url.startswith(("http://", "https://"))
        ),
        None,
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
    path_filter: MediaPathFilter | None = None,
) -> list[OperationPlan]:
    return [item.plan for item in _library_plan_items(repo, library, payload, path_filter)]


def _library_plan_items(
    repo: Repository,
    library: Library,
    payload: OrganizeRequest,
    path_filter: MediaPathFilter | None = None,
) -> list[LibraryPlanItem]:
    organizer = Organizer(repo)
    base_items: list[tuple[MediaAsset, Work, OperationPlan]] = []
    library_root = Path(library.root_path).resolve()
    for asset in repo.list_library_assets(library.id, identified_only=True):
        asset_path = Path(asset.path).resolve()
        if not asset_path.is_relative_to(library_root):
            continue
        if asset.work_id is None:
            continue
        work = repo.get_work(asset.work_id)
        if work is None:
            continue
        if (
            library.recognition_scope == RecognitionScope.JAV_ONLY.value
            and work.family != ContentFamily.JAV.value
        ):
            continue
        if (
            library.recognition_scope == RecognitionScope.JAV_ONLY.value
            and payload.mode is OutputMode.MOVE
            and work.family == ContentFamily.JAV.value
        ):
            if not _strict_jav_organize_candidate(repo, asset, work):
                continue
        elif (
            work.family != ContentFamily.JAV.value
            and not _strict_non_jav_organize_candidate(asset, work)
        ):
            continue
        plan = organizer.plan(
            asset=asset,
            work=work,
            library=library,
            mode=payload.mode,
            target_root=payload.target_root,
            template=payload.template,
        )
        base_items.append((asset, work, plan))

    destination_assets: dict[str, list[str]] = {}
    for asset, _, plan in base_items:
        media_destination = next(
            (operation.destination for operation in plan.operations if operation.detail == "media"),
            None,
        )
        if media_destination is not None:
            destination_assets.setdefault(media_destination.casefold(), []).append(asset.id)
    suffixes = {
        asset_id: f"_{index}"
        for asset_ids in destination_assets.values()
        if len(asset_ids) > 1
        for index, asset_id in enumerate(asset_ids, start=1)
    }

    items: list[LibraryPlanItem] = []
    for asset, work, base_plan in base_items:
        media_suffix = suffixes.get(asset.id)
        plan = base_plan
        if media_suffix is not None:
            plan = organizer.plan(
                asset=asset,
                work=work,
                library=library,
                mode=payload.mode,
                target_root=payload.target_root,
                template=payload.template,
                media_suffix=media_suffix,
            )
        items.append(
            LibraryPlanItem(
                asset=asset,
                work=work,
                media_suffix=media_suffix,
                plan=plan,
            )
        )
    if payload.mode is OutputMode.MOVE and items:
        cleanup_operations = plan_move_cleanup(
            library,
            (operation for item in items for operation in item.plan.operations),
            path_filter or MediaPathFilter(),
        )
        if cleanup_operations:
            last = items[-1]
            updated_plan = organizer.plan(
                asset=last.asset,
                work=last.work,
                library=library,
                mode=payload.mode,
                target_root=payload.target_root,
                template=payload.template,
                media_suffix=last.media_suffix,
                extra_operations=cleanup_operations,
            )
            items[-1] = LibraryPlanItem(
                asset=last.asset,
                work=last.work,
                media_suffix=last.media_suffix,
                plan=updated_plan,
                extra_operations=cleanup_operations,
            )
    return items


def _strict_jav_organize_candidate(
    repo: Repository,
    asset: MediaAsset,
    work: Work,
    *,
    require_cached_artwork: bool = True,
) -> bool:
    """Admit only remotely verified, internally consistent JAV works to bulk moving."""

    if (
        work.family != ContentFamily.JAV.value
        or work.category != MediaCategory.JAPAN.value
        or not work.primary_code
        or not repo.work_has_verified_evidence(work.id)
    ):
        return False
    code, family = extract_code(work.primary_code, MediaCategory.JAPAN)
    if code is None or family is not ContentFamily.JAV:
        return False
    hints = IdentityHints.model_validate(asset.hints)
    if hints.code is None or normalize_identity_value(hints.code) != normalize_identity_value(code):
        return False
    title_source = str((work.field_sources or {}).get("title", ""))
    if title_source in {"", "local-path", "local-manual"}:
        return False
    normalized_title = normalize_title(work.title)
    normalized_code = normalize_title(code)
    title_remainder = normalized_title.replace(normalized_code, "", 1)
    if len("".join(character for character in title_remainder if character.isalnum())) < 4:
        return False
    invalid_actor_names = {"unknown", "uncategorized", "未知", "未分类", "未分類"}
    trusted_actor_sources = {
        "fanza",
        "jav321",
        "mgstage",
        "javlibrary",
        "r18dev",
        "javdb",
        "javbus",
        "airav",
        "avsox",
    }
    if str((work.field_sources or {}).get("actors", "")) not in trusted_actor_sources:
        return False
    valid_actors = [
        actor.strip()
        for actor in work.actors
        if actor.strip() and actor.strip().casefold() not in invalid_actor_names
    ]
    if not valid_actors:
        return False
    searchable_titles = normalize_title(
        " ".join(value for value in (work.title, work.original_title) if value)
    )
    if not any(
        (normalized_actor := normalize_title(actor))
        and len(normalized_actor) >= 2
        and normalized_actor in searchable_titles
        for actor in valid_actors
    ):
        return False
    if require_cached_artwork:
        return any(
            isinstance((local_path := item.get("local_path")), str)
            and Path(local_path).is_file()
            for item in work.artwork
        )
    return any(isinstance(item.get("url"), str) for item in work.artwork)


def _strict_non_jav_organize_candidate(
    asset: MediaAsset,
    work: Work,
    *,
    require_screenshot: bool = True,
) -> bool:
    """Admit only actor-backed local works; real videos also need their own screenshot."""

    expected_category = {
        ContentFamily.CHINESE.value: MediaCategory.CHINA.value,
        ContentFamily.KOREAN.value: MediaCategory.KOREA.value,
        ContentFamily.WESTERN.value: MediaCategory.EUROPE.value,
        ContentFamily.UNKNOWN.value: MediaCategory.OTHER.value,
        ContentFamily.ANIMATION.value: MediaCategory.OTHER.value,
    }.get(work.family)
    if expected_category is None or work.category != expected_category or work.primary_code:
        return False

    hints = IdentityHints.model_validate(asset.hints)
    if hints.code is not None or hints.category.value != work.category:
        return False
    evidence = set(hints.alias_evidence)
    if not evidence.intersection({"directory-actor:confirmed", "directory-actor:catalog"}):
        return False

    invalid_actor_names = {"unknown", "uncategorized", "未知", "未分类", "未分類"}
    work_actors = {
        normalize_title(actor): actor
        for actor in work.actors
        if actor.strip() and actor.strip().casefold() not in invalid_actor_names
    }
    hint_actors = {normalize_title(actor) for actor in hints.actors if actor.strip()}
    matching_actors = hint_actors.intersection(work_actors)
    if not matching_actors:
        return False

    normalized_title = normalize_title(work.title)
    if len("".join(character for character in normalized_title if character.isalnum())) < 2:
        return False
    if is_generic_file_name(Path(asset.path).stem) and not any(
        actor and actor in normalized_title for actor in matching_actors
    ):
        return False

    source = Path(asset.path)
    if source.suffix.casefold() == ".strm":
        return bool(hints.media_locator)
    if not source.is_file():
        return False
    if not require_screenshot:
        return True
    return _asset_generated_artwork_ready(asset, work)


def _asset_generated_artwork_ready(asset: MediaAsset, work: Work) -> bool:
    generated = {
        str(item.get("kind", "")).casefold()
        for item in work.artwork
        if item.get("source") == "local-screenshot"
        and item.get("asset_id") == asset.id
        and isinstance((local_path := item.get("local_path")), str)
        and Path(local_path).is_file()
    }
    return {"fanart", "poster"}.issubset(generated)


def _asset_organize_plan(
    repo: Repository,
    asset: MediaAsset,
    work: Work,
    library: Library,
    payload: OrganizeRequest,
    path_filter: MediaPathFilter,
) -> OperationPlan:
    if (
        library.recognition_scope == RecognitionScope.JAV_ONLY.value
        and payload.mode is OutputMode.MOVE
        and work.family == ContentFamily.JAV.value
    ):
        if not _strict_jav_organize_candidate(repo, asset, work):
            raise ValueError("JAV work is not remotely verified or has incomplete metadata/artwork")
    elif (
        work.family != ContentFamily.JAV.value
        and not _strict_non_jav_organize_candidate(asset, work)
    ):
        requirement = "confirmed directory actor and local screenshot"
        if Path(asset.path).suffix.casefold() == ".strm":
            requirement = "confirmed directory actor and a valid STRM locator"
        raise ValueError(f"non-JAV work requires {requirement}")
    organizer = Organizer(repo)
    plan = organizer.plan(
        asset=asset,
        work=work,
        library=library,
        mode=payload.mode,
        target_root=payload.target_root,
        template=payload.template,
    )
    if payload.mode is not OutputMode.MOVE:
        return plan
    cleanup_operations = plan_move_cleanup(library, plan.operations, path_filter)
    if not cleanup_operations:
        return plan
    return organizer.plan(
        asset=asset,
        work=work,
        library=library,
        mode=payload.mode,
        target_root=payload.target_root,
        template=payload.template,
        extra_operations=cleanup_operations,
    )


def _media_path_filter(request: Request) -> MediaPathFilter:
    return MediaPathFilter(runtime(request).filter_words_store.load().words)


def _library_works(repo: Repository, library: Library) -> list[Work]:
    works: list[Work] = []
    emitted: set[str] = set()
    for asset in repo.list_library_assets(library.id, identified_only=True):
        if asset.work_id is None or asset.work_id in emitted:
            continue
        work = repo.get_work(asset.work_id)
        if work is not None:
            works.append(work)
            emitted.add(work.id)
    return works


async def _ensure_artwork_cached(work: Work, app_runtime: Runtime, repo: Repository) -> None:
    if not work.artwork:
        return
    _, local_paths = await ArtworkStore(
        app_runtime.settings.data_dir / "artwork",
        app_runtime.http,
        max_bytes=app_runtime.settings.artwork_max_bytes,
    ).acquire(work)
    repo.update_artwork_local_paths(work, local_paths)


def _batch_token(plans: list[OperationPlan]) -> str:
    encoded = "\n".join(f"{plan.asset_id}:{plan.token}" for plan in plans).encode()
    return hashlib.sha256(encoded).hexdigest()


def _batch_plan_out(plans: list[OperationPlan]) -> BatchPlanOut:
    operation_count = sum(len(plan.operations) for plan in plans)
    conflict_count = sum(1 for plan in plans for operation in plan.operations if operation.conflict)
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
