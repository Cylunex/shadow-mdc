"""Machine-to-machine API for the MCP server: ``/api/mcp/v1``.

Concise JSON in/out, keyed by 番号. Every route requires
``Authorization: Bearer $SHADOW_MDC_MCP_TOKEN`` (constant-time compare); the
whole router answers 503 when the token is unset. This never touches the web
UI's own auth (nginx OIDC) — it is a separate token for MCP clients only.

All heavy lifting reuses existing services:

* code normalisation → :func:`identity.extract_code`
* catalog create → :meth:`DiscoverService.seed` (JavDB, r18 dump fallback)
* magnets / best pick / 115|OpenList submit → ``seed_auto_offline`` +
  ``pan_offline_enqueue`` (code match → 中字/HD quality → size)
* download completion → :class:`PanOfflinePoller` already exports STRM + NFO +
  artwork + SubtitleCat subtitle and notifies Emby, so the "job" is the
  ``PanOfflineTask`` row (``job_id`` == task id).
"""

from __future__ import annotations

import hmac
import logging
from datetime import date
from pathlib import Path
from typing import Annotated, Any, Literal

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from .api_models import ActorSubscriptionEdit, DiscoverSeedRequest
from .db.models import PanOfflineTask, Work
from .db.repository import Repository
from .domain import IdentityHints, ProviderRecord
from .enums import ContentFamily, MediaCategory, QueryMode
from .identity import extract_code
from .media.magnets import MagnetLink, format_size_bytes, info_hash_from_uri, rank_magnets
from .services.offline_recovery import project_offline
from .services.pan_offline_enqueue import OfflineEnqueueError, enqueue_work_offline
from .services.seed_auto_offline import enqueue_seeded_offline

logger = logging.getLogger(__name__)

SUBTITLE_SUFFIXES = (".srt", ".ass", ".ssa", ".vtt", ".sub")


def _core():  # type: ignore[no-untyped-def]
    # Late import: api.py includes this router at import time.
    from . import api as core

    return core


def _repository(request: Request):  # type: ignore[no-untyped-def]
    with _core().runtime(request).database.session() as session:
        yield Repository(session)


McpRepo = Annotated[Repository, Depends(_repository)]


def require_mcp_token(request: Request) -> None:
    expected = _core().runtime(request).settings.mcp_token
    if not expected:
        raise HTTPException(status_code=503, detail="MCP API disabled (SHADOW_MDC_MCP_TOKEN unset)")
    header = request.headers.get("authorization") or ""
    scheme, _, supplied = header.partition(" ")
    if scheme.lower() != "bearer" or not supplied.strip():
        raise HTTPException(
            status_code=401, detail="missing bearer token", headers={"WWW-Authenticate": "Bearer"}
        )
    if not hmac.compare_digest(expected.encode("utf-8"), supplied.strip().encode("utf-8")):
        raise HTTPException(status_code=401, detail="invalid token", headers={"WWW-Authenticate": "Bearer"})


router = APIRouter(prefix="/api/mcp/v1", tags=["mcp"], dependencies=[Depends(require_mcp_token)])


# ---------------------------------------------------------------- models


class MagnetBrief(BaseModel):
    name: str | None = None
    info_hash: str | None = None
    size: str | None = None
    has_subtitle: bool = False
    hd: bool = False
    provider: str | None = None
    uri: str | None = None


class MetadataBrief(BaseModel):
    code: str | None = None
    title: str | None = None
    original_title: str | None = None
    release_date: date | None = None
    runtime_minutes: int | None = None
    studio: str | None = None
    label: str | None = None
    series: str | None = None
    actors: list[str] = Field(default_factory=list)
    directors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    plot: str | None = None
    cover: str | None = None
    source: str | None = None


class JobOut(BaseModel):
    job_id: str
    work_id: str
    code: str | None = None
    status: str
    stage: str
    progress: float = 0.0
    download_state: str | None = None
    info_hash: str | None = None
    remote_name: str | None = None
    strm_path: str | None = None
    subtitle: bool = False
    error: str | None = None
    created_at: str | None = None
    updated_at: str | None = None


class LibraryOut(BaseModel):
    code: str
    in_catalog: bool
    in_library: bool
    work_id: str | None = None
    strm_path: str | None = None
    local_path: str | None = None
    subtitle: bool = False
    want_list: bool = False
    metadata: MetadataBrief | None = None
    jobs: list[JobOut] = Field(default_factory=list)


class OfflineAddRequest(BaseModel):
    code: str = Field(min_length=2, max_length=100)
    magnet: str | None = Field(default=None, max_length=4000, description="override: magnet/http URL")
    dry_run: bool = False


class OfflineAddOut(BaseModel):
    code: str
    action: Literal["exists", "running", "submitted", "planned", "hunting", "error"]
    dry_run: bool
    work_id: str | None = None
    job_id: str | None = None
    job: JobOut | None = None
    metadata: MetadataBrief | None = None
    magnet: MagnetBrief | None = None
    candidates: list[MagnetBrief] = Field(default_factory=list)
    message: str = ""


class MetadataOut(BaseModel):
    code: str
    in_catalog: bool
    work_id: str | None = None
    r18dump: MetadataBrief | None = None
    providers: list[MetadataBrief] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)


class SubscribeRequest(BaseModel):
    code: str | None = Field(default=None, max_length=100)
    actor: str | None = Field(default=None, max_length=200)
    start_date: date | None = None
    max_cast: int = Field(default=3, ge=1, le=50)


class SubscribeOut(BaseModel):
    kind: Literal["code", "actor"]
    target: str
    work_id: str | None = None
    created: bool
    message: str = ""


class RescrapeRequest(BaseModel):
    code: str = Field(min_length=2, max_length=100)


class RescrapeOut(BaseModel):
    code: str
    work_id: str
    updated: bool
    accepted: list[str] = Field(default_factory=list)
    failures: list[str] = Field(default_factory=list)
    metadata: MetadataBrief | None = None


# ---------------------------------------------------------------- helpers


def normalize_code_or_422(raw: str) -> str:
    code, _family = extract_code(raw.strip())
    if code is None:
        raise HTTPException(status_code=422, detail=f"not a recognised 番号: {raw!r}")
    return code


def _cover(artwork: Any) -> str | None:
    for item in artwork or ():
        if isinstance(item, dict):
            kind, url = item.get("kind"), item.get("url")
        else:
            kind, url = getattr(item, "kind", None), getattr(item, "url", None)
        kind = getattr(kind, "value", kind)
        if url and kind in {"poster", "cover", "fanart", None}:
            return str(url)
    return None


def _plot(text: str | None) -> str | None:
    if not text:
        return None
    return text if len(text) <= 400 else text[:400] + "…"


def brief_from_record(record: ProviderRecord) -> MetadataBrief:
    return MetadataBrief(
        code=record.code,
        title=record.title,
        original_title=record.original_title,
        release_date=record.release_date,
        runtime_minutes=(record.runtime_seconds // 60) if record.runtime_seconds else None,
        studio=record.studio,
        label=record.label,
        series=record.series,
        actors=list(record.actors),
        directors=list(record.directors),
        tags=list(record.tags)[:30],
        plot=_plot(record.plot),
        cover=_cover(record.artwork),
        source=record.provider,
    )


def brief_from_work(work: Work) -> MetadataBrief:
    return MetadataBrief(
        code=work.primary_code,
        title=work.title,
        original_title=work.original_title,
        release_date=work.release_date,
        runtime_minutes=(work.runtime_seconds // 60) if work.runtime_seconds else None,
        studio=work.studio,
        label=work.label,
        series=work.series,
        actors=list(work.actors or []),
        directors=list(work.directors or []),
        tags=[t for t in (work.tags or []) if not str(t).startswith(("daily-", "weekly-"))][:30],
        plot=_plot(work.plot),
        cover=_cover(work.artwork),
        source="catalog",
    )


def _magnet_brief(item: Any, *, with_uri: bool = False) -> MagnetBrief:
    uri = getattr(item, "uri", None)
    info_hash = getattr(item, "info_hash", None) or (info_hash_from_uri(uri) if uri else None)
    return MagnetBrief(
        name=getattr(item, "name", None),
        info_hash=info_hash,
        size=format_size_bytes(getattr(item, "size_bytes", None)),
        has_subtitle=bool(getattr(item, "has_subtitle", False)),
        hd=bool(getattr(item, "hd", False)),
        provider=getattr(item, "provider", None),
        uri=uri if with_uri else None,
    )


def _has_subtitle(strm_path: str | None) -> bool:
    if not strm_path:
        return False
    path = Path(strm_path)
    try:
        parent = path.parent
        if not parent.is_dir():
            return False
        return any(child.suffix.lower() in SUBTITLE_SUFFIXES for child in parent.iterdir() if child.is_file())
    except OSError:
        return False


def _stage(task: PanOfflineTask) -> str:
    if task.status == "running":
        return "downloading"
    if task.status in {"done", "completed"}:
        return "in_library" if task.strm_path else "organizing"
    return task.status


def job_out(task: PanOfflineTask, work: Work | None) -> JobOut:
    projection = project_offline(task)
    return JobOut(
        job_id=task.id,
        work_id=task.work_id,
        code=work.primary_code if work is not None else None,
        status=task.status,
        stage=_stage(task),
        progress=float(task.progress or 0.0),
        download_state=projection.download_state or None,
        info_hash=task.info_hash or None,
        remote_name=task.remote_name,
        strm_path=task.strm_path,
        subtitle=_has_subtitle(task.strm_path),
        error=task.error,
        created_at=task.created_at.isoformat() if task.created_at else None,
        updated_at=task.updated_at.isoformat() if task.updated_at else None,
    )


def _jobs_for(repo: Repository, work: Work) -> list[JobOut]:
    return [job_out(task, work) for task in repo.list_pan_offline_tasks(work_id=work.id, limit=20)]


def library_state(repo: Repository, code: str, *, want_list: set[str]) -> LibraryOut:
    work = repo.find_work_by_code(code)
    if work is None:
        return LibraryOut(code=code, in_catalog=False, in_library=False)
    jobs = _jobs_for(repo, work)
    done = next((job for job in jobs if job.stage == "in_library"), None)
    asset = repo.first_asset_for_work(work.id)
    return LibraryOut(
        code=code,
        in_catalog=True,
        in_library=done is not None or asset is not None,
        work_id=work.id,
        strm_path=done.strm_path if done else None,
        local_path=asset.path if asset is not None else None,
        subtitle=bool(done and done.subtitle),
        want_list=work.id in want_list,
        metadata=brief_from_work(work),
        jobs=jobs,
    )


def _jav_hints(code: str) -> IdentityHints:
    parsed, family = extract_code(code)
    return IdentityHints(
        term=parsed or code,
        mode=QueryMode.CODE,
        family=family,
        category=MediaCategory.JAPAN if family is ContentFamily.JAV else MediaCategory.OTHER,
        code=parsed or code,
    )


async def _preview_magnets(
    app_runtime: Any, repo: Repository, code: str, work: Work | None
) -> list[MagnetLink | Any]:
    """Read-only magnet discovery (dry run): stored → javdb → sukebei; nothing persisted."""

    if work is not None:
        stored = list(repo.list_work_magnets(work.id))
        if stored:
            return stored
    discover = app_runtime.discover
    found: list[MagnetLink] = []
    try:
        batch = await app_runtime.providers.search(_jav_hints(code), provider_ids=("javdb",))
        for record in batch.records:
            if record.provider == "javdb":
                found.extend(
                    await discover.list_magnets(
                        provider="javdb", external_id=record.external_id, source_url=record.source_url
                    )
                )
                break
    except Exception as exc:
        logger.info("mcp dry-run javdb magnets %s: %s", code, type(exc).__name__)
    if not found and discover.sukebei_available:
        try:
            found.extend(await discover.list_magnets(provider="sukebei", external_id=code))
        except Exception as exc:
            logger.info("mcp dry-run sukebei magnets %s: %s", code, type(exc).__name__)
    return found


def _rank(items: list[Any], code: str) -> list[Any]:
    links = [item for item in items if isinstance(item, MagnetLink)]
    if links and len(links) == len(items):
        return rank_magnets(links, expected_code=code)
    from .media.magnets import magnet_sort_key

    return sorted(
        items,
        key=lambda item: magnet_sort_key(
            name=item.name,
            size_bytes=item.size_bytes,
            has_subtitle=bool(item.has_subtitle),
            hd=bool(item.hd),
            expected_code=code,
        ),
        reverse=True,
    )


# ---------------------------------------------------------------- routes


@router.get("/health")
def mcp_health(request: Request) -> dict[str, object]:
    app_runtime = _core().runtime(request)
    pan_status = app_runtime.pan_service.status()
    return {
        "ok": True,
        "pan_backend": app_runtime.pan_service.backend(),
        "pan_connected": bool(pan_status.get("connected")),
        "offline_target_set": bool(app_runtime.pan_service.offline_target()),
        "r18dump": app_runtime.discover.r18_fallback_available,
    }


@router.get("/library/{code}", response_model=LibraryOut)
def mcp_library(code: str, request: Request, repo: McpRepo) -> LibraryOut:
    """Is 番号 in the catalog / library (STRM or local file)? Metadata + subtitle + jobs."""

    normalized = normalize_code_or_422(code)
    want = set(_core().runtime(request).library_prefs_store.load().want_list)
    return library_state(repo, normalized, want_list=want)


@router.get("/metadata/{code}", response_model=MetadataOut)
async def mcp_metadata(
    code: str,
    request: Request,
    repo: McpRepo,
    providers: bool = Query(False, description="also query remote providers (slower, network)"),
) -> MetadataOut:
    """Metadata by 番号 without adding anything: catalog + r18 dump (+ providers)."""

    normalized = normalize_code_or_422(code)
    app_runtime = _core().runtime(request)
    work = repo.find_work_by_code(normalized)
    record = app_runtime.discover.r18_record(normalized)
    out = MetadataOut(
        code=normalized,
        in_catalog=work is not None,
        work_id=work.id if work is not None else None,
        r18dump=brief_from_record(record) if record is not None else None,
    )
    if providers or (record is None and work is None):
        batch = await app_runtime.providers.search(_jav_hints(normalized))
        out.providers = [brief_from_record(item) for item in batch.records[:5]]
        out.failures = [f"{item.provider}: {item.reason}" for item in batch.failures]
    if work is not None:
        out.providers.insert(0, brief_from_work(work))
    return out


@router.post("/offline", response_model=OfflineAddOut)
async def mcp_offline_add(payload: OfflineAddRequest, request: Request, repo: McpRepo) -> OfflineAddOut:
    """番号 → (catalog seed) → best magnet → 115/OpenList offline → poller organizes STRM/NFO/subtitle.

    Idempotent: returns the existing library entry or running job instead of resubmitting.
    """

    core = _core()
    app_runtime = core.runtime(request)
    code = normalize_code_or_422(payload.code)
    want = set(app_runtime.library_prefs_store.load().want_list)
    state = library_state(repo, code, want_list=want)
    if state.in_library:
        return OfflineAddOut(
            code=code,
            action="exists",
            dry_run=payload.dry_run,
            work_id=state.work_id,
            job_id=state.jobs[0].job_id if state.jobs else None,
            job=state.jobs[0] if state.jobs else None,
            metadata=state.metadata,
            message="already in library",
        )
    running = next((job for job in state.jobs if job.status == "running"), None)
    if running is not None and not payload.magnet:
        return OfflineAddOut(
            code=code,
            action="running",
            dry_run=payload.dry_run,
            work_id=state.work_id,
            job_id=running.job_id,
            job=running,
            metadata=state.metadata,
            message="offline download already running",
        )

    work = repo.find_work_by_code(code)
    pan_status = app_runtime.pan_service.status()
    pan_ready = bool(pan_status.get("connected")) and bool(app_runtime.pan_service.offline_target())

    if payload.dry_run:
        metadata = state.metadata
        if metadata is None:
            record = app_runtime.discover.r18_record(code)
            if record is None:
                batch = await app_runtime.providers.search(_jav_hints(code))
                record = batch.records[0] if batch.records else None
            metadata = brief_from_record(record) if record is not None else None
        if payload.magnet:
            chosen = MagnetBrief(
                uri=payload.magnet, info_hash=info_hash_from_uri(payload.magnet), provider="override"
            )
            ranked_briefs: list[MagnetBrief] = []
        else:
            ranked = _rank(await _preview_magnets(app_runtime, repo, code, work), code)
            ranked_briefs = [_magnet_brief(item) for item in ranked[:5]]
            chosen = _magnet_brief(ranked[0], with_uri=True) if ranked else None
        steps = []
        if work is None:
            steps.append("seed catalog work" + (" (metadata found)" if metadata else " (NO metadata found)"))
        steps.append("submit 115 offline" if chosen else "no magnet → add to want_list (盯磁 keeps hunting)")
        steps.append("poller on completion: STRM + NFO + artwork + subtitle + Emby notify")
        if not pan_ready:
            steps.append("WARNING: pan/offline target not ready")
        return OfflineAddOut(
            code=code,
            action="planned" if chosen else "hunting",
            dry_run=True,
            work_id=work.id if work is not None else None,
            metadata=metadata,
            magnet=chosen,
            candidates=ranked_briefs,
            message="; ".join(steps),
        )

    if not pan_ready:
        raise HTTPException(
            status_code=400, detail="pan/OpenList offline not ready (login or offline target missing)"
        )

    if work is None:
        try:
            await core.discover_seed(
                DiscoverSeedRequest(provider="javdb", code=code, auto_offline=False), request, repo
            )
        except HTTPException as exc:
            if exc.status_code != 404:
                raise
            raise HTTPException(status_code=404, detail=f"no metadata for {code}; cannot seed") from exc
        work = repo.find_work_by_code(code)
        if work is None:
            raise HTTPException(status_code=500, detail="seed did not create a work")
    work_id = work.id
    repo._session.commit()

    if payload.magnet:
        try:
            result = await enqueue_work_offline(repo, app_runtime.pan_service, work_id, url=payload.magnet)
        except OfflineEnqueueError as exc:
            raise HTTPException(status_code=exc.status_code, detail=exc.message) from exc
        repo._session.commit()
        app_runtime.task_events.notify()
        job = job_out(result.task, work)
        return OfflineAddOut(
            code=code,
            action="running" if result.reused_running else "submitted",
            dry_run=False,
            work_id=work_id,
            job_id=job.job_id,
            job=job,
            metadata=brief_from_work(work),
            magnet=MagnetBrief(uri=payload.magnet, info_hash=result.task.info_hash, provider="override"),
        )

    stats = await enqueue_seeded_offline(
        database=app_runtime.database,
        pan=app_runtime.pan_service,
        discover=app_runtime.discover,
        work_ids=[work_id],
        pace_seconds=0,
        library_prefs_store=app_runtime.library_prefs_store,
    )
    app_runtime.task_events.notify()
    row = stats.results[0] if stats.results else {}
    outcome = str(row.get("outcome") or "error")
    reason = str(row.get("reason") or "")
    repo._session.expire_all()
    work = repo.get_work(work_id) or work
    tasks = repo.list_pan_offline_tasks(work_id=work_id, limit=1)
    job = job_out(tasks[0], work) if tasks else None
    action: str = {
        "submitted": "submitted",
        "reused": "running",
        "skipped": "running" if job is not None else "error",
        "hunting": "hunting",
    }.get(outcome, "error")
    if action == "hunting":
        reason = (reason + "; added to want_list (盯磁)").strip("; ")
    return OfflineAddOut(
        code=code,
        action=action,
        dry_run=False,
        work_id=work_id,  # type: ignore[arg-type]
        job_id=job.job_id if job and action != "hunting" else None,
        job=job if action != "hunting" else None,
        metadata=brief_from_work(work),
        message=reason,
    )


@router.get("/jobs", response_model=list[JobOut])
def mcp_jobs(
    repo: McpRepo,
    status: str | None = Query(None, pattern="^(running|done|failed|cancelled)$"),
    limit: int = Query(20, ge=1, le=100),
) -> list[JobOut]:
    tasks = repo.list_pan_offline_tasks(status=status, limit=limit)
    works: dict[str, Work | None] = {}
    out: list[JobOut] = []
    for task in tasks:
        if task.work_id not in works:
            works[task.work_id] = repo.get_work(task.work_id)
        out.append(job_out(task, works[task.work_id]))
    return out


@router.get("/jobs/{job_id}", response_model=JobOut)
def mcp_job(job_id: str, repo: McpRepo) -> JobOut:
    task = repo.get_pan_offline_task(job_id)
    if task is None:
        raise HTTPException(status_code=404, detail="job not found")
    return job_out(task, repo.get_work(task.work_id))


@router.post("/subscribe", response_model=SubscribeOut)
async def mcp_subscribe(payload: SubscribeRequest, request: Request, repo: McpRepo) -> SubscribeOut:
    """盯磁: 番号 → want_list (watcher refreshes magnets & auto-offlines); 演员 → actor subscription."""

    core = _core()
    app_runtime = core.runtime(request)
    store = app_runtime.library_prefs_store
    if bool(payload.code) == bool(payload.actor):
        raise HTTPException(status_code=422, detail="pass exactly one of code / actor")
    if payload.code:
        code = normalize_code_or_422(payload.code)
        work = repo.find_work_by_code(code)
        if work is None:
            try:
                await core.discover_seed(
                    DiscoverSeedRequest(provider="javdb", code=code, auto_offline=False), request, repo
                )
            except HTTPException as exc:
                raise HTTPException(
                    status_code=exc.status_code, detail=f"cannot seed {code}: {exc.detail}"
                ) from exc
            work = repo.find_work_by_code(code)
            if work is None:
                raise HTTPException(status_code=500, detail="seed did not create a work")
        before = set(store.load().want_list)
        store.add_wants([work.id])
        return SubscribeOut(
            kind="code",
            target=code,
            work_id=work.id,
            created=work.id not in before,
            message="watched by 盯磁 (subscription watcher)",
        )
    actor = (payload.actor or "").strip()
    if not actor:
        raise HTTPException(status_code=422, detail="actor is empty")
    existing = next(
        (item for item in store.load().subscriptions if actor in {item.actor_name, item.actor_key}), None
    )
    if existing is not None:
        return SubscribeOut(kind="actor", target=actor, created=False, message="already subscribed")
    core.put_actor_subscription(
        ActorSubscriptionEdit(
            actor_key=actor,
            actor_name=actor,
            start_date=payload.start_date or date.today(),
            max_cast=payload.max_cast,
        ),
        request,
        repo,
    )
    return SubscribeOut(kind="actor", target=actor, created=True, message="actor subscribed")


@router.post("/rescrape", response_model=RescrapeOut)
async def mcp_rescrape(payload: RescrapeRequest, request: Request, repo: McpRepo) -> RescrapeOut:
    """Forced rescrape by 番号 (respects field locks → 409 and daily quota → 429)."""

    code = normalize_code_or_422(payload.code)
    work = repo.find_work_by_code(code)
    if work is None:
        raise HTTPException(status_code=404, detail=f"{code} not in catalog")
    result = await _core().refresh_work_metadata(work.id, request, repo)
    refreshed = repo.get_work(work.id) or work
    return RescrapeOut(
        code=code,
        work_id=work.id,
        updated=result.accepted_work_id is not None,
        accepted=list(result.candidate_ids),
        failures=[f"{item.provider}: {item.reason}" for item in result.failures],
        metadata=brief_from_work(refreshed),
    )
