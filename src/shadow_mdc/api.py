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
    CandidateOut,
    HealthOut,
    IdentifyOut,
    IdentifyRequest,
    IdentityOut,
    LibraryCreate,
    LibraryOut,
    OrganizeApplyRequest,
    OrganizeRequest,
    PlanOut,
    ProviderListOut,
    ScanOut,
    WorkOut,
)
from .config import Settings
from .db.models import Library, MatchCandidateRow, MediaAsset, Work
from .db.repository import Database, Repository
from .domain import IdentityHints, ProviderRecord
from .enums import QueryMode
from .identity import build_identity_hints
from .media.nfo import build_nfo
from .media.organizer import Organizer
from .providers import JavBusProvider, JavDBProvider, JsonLdProvider, ProviderRegistry, ThePornDBProvider
from .services.identify import IdentifyService
from .services.scanner import Scanner


@dataclass(frozen=True)
class Runtime:
    settings: Settings
    database: Database
    http: httpx.AsyncClient
    providers: ProviderRegistry


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
    )
    providers = ProviderRegistry(
        [
            JavDBProvider(client, settings.javdb_base_url),
            JavBusProvider(client, settings.javbus_base_url),
            ThePornDBProvider(client, settings.theporndb_graphql_url, settings.theporndb_token),
            JsonLdProvider(client),
        ]
    )
    app.state.runtime = Runtime(settings=settings, database=database, http=client, providers=providers)
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


@app.get("/api/health", response_model=HealthOut)
def health() -> HealthOut:
    return HealthOut(version=__version__)


@app.get("/api/providers", response_model=ProviderListOut)
def providers(request: Request) -> ProviderListOut:
    return ProviderListOut(providers=runtime(request).providers.descriptors())


@app.post("/api/libraries", response_model=LibraryOut, status_code=status.HTTP_201_CREATED)
def create_library(payload: LibraryCreate, repo: Repo) -> LibraryOut:
    root = Path(payload.root_path).resolve()
    if not root.is_dir():
        raise HTTPException(status_code=422, detail="root_path must be an existing directory")
    try:
        library = repo.create_library(
            name=payload.name,
            root_path=str(root),
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
def scan_library(library_id: str, repo: Repo) -> ScanOut:
    library = repo.get_library(library_id)
    if library is None:
        raise HTTPException(status_code=404, detail="library not found")
    try:
        result = Scanner(repo).scan(library)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
        hints = _override_hints(asset.path, IdentityHints.model_validate(asset.hints), payload)
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


@app.get("/api/works/{work_id}", response_model=WorkOut)
def get_work(work_id: str, repo: Repo) -> WorkOut:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    return _work_out(repo, work)


@app.get("/api/works/{work_id}/nfo", response_class=PlainTextResponse)
def work_nfo(work_id: str, repo: Repo) -> Response:
    work = repo.get_work(work_id)
    if work is None:
        raise HTTPException(status_code=404, detail="work not found")
    return PlainTextResponse(build_nfo(work, repo.identities_for_work(work.id)), media_type="application/xml")


@app.post("/api/assets/{asset_id}/organize/plan", response_model=PlanOut)
def organize_plan(asset_id: str, payload: OrganizeRequest, repo: Repo) -> PlanOut:
    asset, work, library = _organize_entities(repo, asset_id)
    try:
        plan = Organizer(repo).plan(asset=asset, work=work, library=library, mode=payload.mode)
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
            replace_nfo=payload.replace_nfo,
        )
    except (ValueError, FileExistsError, OSError) as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return PlanOut.model_validate(plan.model_dump())


def _override_hints(path: str, current: IdentityHints, payload: IdentifyRequest) -> IdentityHints:
    if payload.source_url or payload.external_ids:
        return build_identity_hints(
            path,
            source_url=payload.source_url,
            external_ids=payload.external_ids,
            fingerprints=current.fingerprints,
            duration_seconds=current.duration_seconds,
        )
    if payload.title:
        return current.model_copy(
            update={"term": payload.title, "title": payload.title, "mode": QueryMode.TEXT}
        )
    return current


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


source_web_dist = Path(__file__).resolve().parents[2] / "web" / "dist"
packaged_web_dist = Path(__file__).resolve().parent / "static"
web_dist = source_web_dist if source_web_dist.is_dir() else packaged_web_dist
if web_dist.is_dir():
    app.mount("/", StaticFiles(directory=web_dist, html=True), name="web")
