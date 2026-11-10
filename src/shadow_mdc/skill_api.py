"""Authenticated, allowlisted HTTP capabilities for the MDC operator skill."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    JsonValue,
    TypeAdapter,
    ValidationError,
    field_validator,
    model_validator,
)

from .api_models import BulkIdentifyRequest, IdentifyRequest, WorkLookupRequest
from .catalog_api import TaxonomyEdit
from .catalog_management import CatalogFilter
from .db.models import Work

if TYPE_CHECKING:
    from .api import Runtime


class EmptyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")


class WorkInput(EmptyInput):
    work_id: str = Field(min_length=1, max_length=240)

    @field_validator("work_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


def safe_id(value: str) -> str:
    """Keep identifiers in their route segment rather than accepting a URL or path."""
    if value in {".", ".."} or any(char in value for char in "/\\?#") or any(ord(c) < 32 for c in value):
        raise ValueError("invalid identifier")
    return value


class TaskInput(EmptyInput):
    task_id: str = Field(min_length=1, max_length=240)

    @field_validator("task_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class PlaylistInput(EmptyInput):
    playlist_id: str = Field(min_length=1, max_length=240)

    @field_validator("playlist_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class ActorsInput(EmptyInput):
    q: str = Field(default="", max_length=300)
    sort: Literal["works", "name"] = "works"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=48, ge=1, le=100)
    followed: bool = False


class TagsInput(EmptyInput):
    only_with_works: bool = False


class TasksInput(EmptyInput):
    limit: int = Field(default=30, ge=1, le=100)


class WantedInput(WorkInput):
    wanted: bool


class TaxonomyInput(TaxonomyEdit, WorkInput):
    pass


class CodeInput(EmptyInput):
    code: str = Field(min_length=2, max_length=100)

    @field_validator("code")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class OfflineCodeInput(CodeInput):
    magnet: str | None = Field(default=None, min_length=3, max_length=4000)


class CodePreviewInput(OfflineCodeInput):
    dry_run: Literal[True] = True


class CodeSubmitInput(OfflineCodeInput):
    dry_run: Literal[False] = False


class MagnetInput(EmptyInput):
    magnet: str = Field(min_length=20, max_length=4000)
    title: str | None = Field(default=None, max_length=500)
    code: str | None = Field(default=None, max_length=100)


class MagnetPreviewInput(MagnetInput):
    dry_run: Literal[True] = True


class MagnetSubmitInput(MagnetInput):
    dry_run: Literal[False] = False


class IntakeJobsInput(EmptyInput):
    status: Literal["running", "done", "failed", "cancelled"] | None = None
    limit: int = Field(default=20, ge=1, le=100)


class IntakeJobInput(EmptyInput):
    job_id: str = Field(min_length=1, max_length=240)

    @field_validator("job_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class LookupInput(WorkLookupRequest, EmptyInput):
    @model_validator(mode="after")
    def one_identity(self) -> LookupInput:
        if sum(bool(item) for item in (self.code, self.source_url, self.external_ids)) != 1:
            raise ValueError("provide exactly one of code, source_url or external_ids")
        return self


class LibraryInput(EmptyInput):
    library_id: str = Field(min_length=1, max_length=240)

    @field_validator("library_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class LibraryAssetsInput(LibraryInput):
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=48, ge=1, le=100)
    state: str | None = Field(default=None, max_length=100)


class LibraryScanInput(LibraryInput):
    only_new: bool = True


class LibraryIdentifyInput(BulkIdentifyRequest, LibraryInput):
    pass


class AssetInput(EmptyInput):
    asset_id: str = Field(min_length=1, max_length=240)

    @field_validator("asset_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class AssetIdentifyInput(IdentifyRequest, AssetInput):
    pass


class CandidateInput(EmptyInput):
    candidate_id: str = Field(min_length=1, max_length=240)

    @field_validator("candidate_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


RouteKey = Literal[
    "work_id", "task_id", "playlist_id", "code", "job_id", "library_id", "asset_id", "candidate_id"
]


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    effect: Literal["read", "write"]
    model: type[BaseModel]
    method: Literal["GET", "POST", "PUT"]
    path: str
    path_key: RouteKey | None = None


CAPABILITIES: tuple[Capability, ...] = (
    Capability("system.health", "检查 MDC 服务状态。", "read", EmptyInput, "GET", "/api/health"),
    Capability(
        "works.query",
        "按片名、演员、标签、分类和状态分页查询作品。",
        "read",
        CatalogFilter,
        "POST",
        "/api/catalog/works/query",
    ),
    Capability(
        "works.facets",
        "读取当前筛选下的标签、分类、机构和年份计数。",
        "read",
        CatalogFilter,
        "POST",
        "/api/catalog/facets",
    ),
    Capability(
        "works.detail",
        "按作品 ID 读取详情及已有资源。",
        "read",
        WorkInput,
        "GET",
        "/api/works/{work_id}",
        "work_id",
    ),
    Capability(
        "actors.query",
        "按姓名或别名分页查询演员档案。",
        "read",
        ActorsInput,
        "GET",
        "/api/catalog/people-cards",
    ),
    Capability(
        "tags.list", "浏览带封面的大标签、别名和作品计数。", "read", TagsInput, "GET", "/api/categories"
    ),
    Capability(
        "classifications.list",
        "读取七个分类维度的正式 ID、名称及计数。",
        "read",
        EmptyInput,
        "GET",
        "/api/catalog/classifications",
    ),
    Capability(
        "classifications.work",
        "读取作品当前分类与人工发行年。",
        "read",
        WorkInput,
        "GET",
        "/api/catalog/works/{work_id}/taxonomy",
        "work_id",
    ),
    Capability(
        "classifications.assign",
        "按明确要求设置指定维度的分类。省略维度保持原值。",
        "write",
        TaxonomyInput,
        "PUT",
        "/api/catalog/works/{work_id}/taxonomy",
        "work_id",
    ),
    Capability(
        "works.want",
        "按明确要求设置或取消作品想看状态。",
        "write",
        WantedInput,
        "PUT",
        "/api/library-prefs/want-list",
    ),
    Capability(
        "playlists.list",
        "列出当前偏好空间的人工与动态片单。",
        "read",
        EmptyInput,
        "GET",
        "/api/catalog/playlists",
    ),
    Capability(
        "playlists.works",
        "读取当前偏好空间中指定片单的作品。",
        "read",
        PlaylistInput,
        "GET",
        "/api/catalog/playlists/{playlist_id}/works",
        "playlist_id",
    ),
    Capability("tasks.list", "读取最近任务的实际状态。", "read", TasksInput, "GET", "/api/tasks"),
    Capability(
        "tasks.get",
        "按任务 ID 核对执行状态及结果。",
        "read",
        TaskInput,
        "GET",
        "/api/tasks/{task_id}",
        "task_id",
    ),
    Capability(
        "intake.status",
        "按番号核对建档、媒体就绪及离线任务。",
        "read",
        CodeInput,
        "GET",
        "/api/intake/v1/library/{code}",
        "code",
    ),
    Capability(
        "intake.code.preview",
        "预览番号入库及候选磁力、不写库、不提交下载。",
        "read",
        CodePreviewInput,
        "POST",
        "/api/intake/v1/offline",
    ),
    Capability(
        "intake.code.submit",
        "按明确要求建档并提交番号离线入库。无磁力时转为盯磁。",
        "write",
        CodeSubmitInput,
        "POST",
        "/api/intake/v1/offline",
    ),
    Capability(
        "intake.magnet.inspect",
        "解析磁力的编号、名称、清晰度和现有任务、不写库。",
        "read",
        MagnetInput,
        "POST",
        "/api/intake/v1/magnet/inspect",
    ),
    Capability(
        "intake.magnet.preview",
        "预览有番号或无番号磁力入库、不写库、不提交下载。",
        "read",
        MagnetPreviewInput,
        "POST",
        "/api/intake/v1/offline/magnet",
    ),
    Capability(
        "intake.magnet.submit",
        "按明确要求将磁力入库、复用同哈希任务并在完成后导出媒体。",
        "write",
        MagnetSubmitInput,
        "POST",
        "/api/intake/v1/offline/magnet",
    ),
    Capability(
        "intake.jobs",
        "分页上限内查询离线入库任务及 STRM 导出状态。",
        "read",
        IntakeJobsInput,
        "GET",
        "/api/intake/v1/jobs",
    ),
    Capability(
        "intake.job",
        "按入库任务 ID 核对下载与媒体导出结果。",
        "read",
        IntakeJobInput,
        "GET",
        "/api/intake/v1/jobs/{job_id}",
        "job_id",
    ),
    Capability(
        "works.lookup",
        "按编号、来源 URL 或外部 ID 刮削并建档元数据、不提交下载。",
        "write",
        LookupInput,
        "POST",
        "/api/works/lookup",
    ),
    Capability(
        "works.refresh",
        "重新刮削已有作品、沿用字段锁和服务配额。",
        "write",
        WorkInput,
        "POST",
        "/api/works/{work_id}/refresh",
        "work_id",
    ),
    Capability("libraries.list", "列出已配置的本地媒体库。", "read", EmptyInput, "GET", "/api/libraries"),
    Capability(
        "libraries.assets",
        "分页读取指定媒体库的资产、含扫描后待识别条目。",
        "read",
        LibraryAssetsInput,
        "GET",
        "/api/intake/v1/libraries/{library_id}/assets",
        "library_id",
    ),
    Capability(
        "libraries.scan",
        "提交本地媒体扫描任务、默认只扫描新增文件。",
        "write",
        LibraryScanInput,
        "POST",
        "/api/libraries/{library_id}/scan/jobs",
        "library_id",
    ),
    Capability(
        "libraries.identify",
        "提交媒体库批量识别任务、低置信候选留待用户选择。",
        "write",
        LibraryIdentifyInput,
        "POST",
        "/api/libraries/{library_id}/identify/jobs",
        "library_id",
    ),
    Capability(
        "assets.get",
        "读取本地资产及其作品关联状态。",
        "read",
        AssetInput,
        "GET",
        "/api/assets/{asset_id}",
        "asset_id",
    ),
    Capability(
        "assets.identify",
        "识别单个已扫描资产、可使用用户提供的标题、URL 或外部 ID。",
        "write",
        AssetIdentifyInput,
        "POST",
        "/api/assets/{asset_id}/identify",
        "asset_id",
    ),
    Capability(
        "assets.candidates",
        "读取指定资产的匹配候选与证据。",
        "read",
        AssetInput,
        "GET",
        "/api/assets/{asset_id}/candidates",
        "asset_id",
    ),
    Capability(
        "candidates.accept",
        "接受用户明确选择的候选并关联作品、不移动原文件。",
        "write",
        CandidateInput,
        "POST",
        "/api/candidates/{candidate_id}/accept",
        "candidate_id",
    ),
)
_INDEX = {capability.name: capability for capability in CAPABILITIES}
_JSON: TypeAdapter[JsonValue] = TypeAdapter(JsonValue)


class CapabilityOut(BaseModel):
    name: str
    description: str
    effect: Literal["read", "write"]
    input_schema: dict[str, JsonValue]


class CatalogOut(BaseModel):
    version: str = "1"
    capabilities: list[CapabilityOut]


class SkillCall(EmptyInput):
    name: str = Field(min_length=1, max_length=100)
    arguments: dict[str, JsonValue] = Field(default_factory=dict)


class SkillResult(BaseModel):
    ok: bool
    capability: str
    status: int
    result: JsonValue


def build_skill_router(runtime: Callable[[Request], Runtime]) -> APIRouter:
    """Reuse domain endpoints through an authenticated, finite HTTP allowlist; no MCP runtime."""

    def authorize(request: Request) -> None:
        expected = runtime(request).settings.mcp_token
        if not expected:
            raise HTTPException(503, "MDC skill API is not configured")
        scheme, _, supplied = request.headers.get("authorization", "").partition(" ")
        if (
            scheme.lower() != "bearer"
            or not supplied
            or not hmac.compare_digest(
                expected.encode(),
                supplied.strip().encode(),
            )
        ):
            raise HTTPException(401, "invalid service token", headers={"WWW-Authenticate": "Bearer"})

    router = APIRouter(prefix="/api/skill/v1", dependencies=[Depends(authorize)])

    @router.get("/catalog")
    def catalog() -> CatalogOut:
        return CatalogOut(
            capabilities=[
                CapabilityOut(
                    name=item.name,
                    description=item.description,
                    effect=item.effect,
                    input_schema={
                        key: _JSON.validate_python(value)
                        for key, value in item.model.model_json_schema().items()
                    },
                )
                for item in CAPABILITIES
            ]
        )

    @router.post("/call")
    async def call(
        payload: SkillCall,
        request: Request,
        owner: Annotated[str, Header(alias="X-Catalog-Owner", pattern=r"^[A-Za-z0-9_-]{1,100}$")],
    ) -> SkillResult:
        capability = _INDEX.get(payload.name)
        if capability is None:
            raise HTTPException(404, "unknown MDC capability")
        try:
            parsed = capability.model.model_validate(payload.arguments)
        except ValidationError as exc:
            raise HTTPException(
                422,
                detail=[
                    {"field": ".".join(str(part) for part in error["loc"]), "type": error["type"]}
                    for error in exc.errors(include_input=False, include_context=False)
                ],
            ) from exc
        if capability.name == "works.want" and isinstance(parsed, WantedInput):
            with runtime(request).database.session() as session:
                if session.get(Work, parsed.work_id) is None:
                    return SkillResult(
                        ok=False,
                        capability=capability.name,
                        status=404,
                        result={"detail": "work not found"},
                    )
        values = parsed.model_dump(mode="json", exclude_unset=isinstance(parsed, TaxonomyInput))
        path = capability.path
        if capability.path_key:
            identity = values.pop(capability.path_key)
            path = path.format(**{capability.path_key: quote(str(identity), safe="")})
        headers = {"X-Catalog-Owner": owner}
        if path.startswith("/api/intake/v1/"):
            headers["Authorization"] = request.headers["authorization"]
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app), base_url="http://mdc"
        ) as client:
            response = await client.request(
                capability.method,
                path,
                headers=headers,
                params={key: value for key, value in values.items() if value is not None}
                if capability.method == "GET"
                else None,
                json=values if capability.method != "GET" else None,
            )
        result = _JSON.validate_python(response.json())
        if capability.name == "works.detail" and response.is_success and isinstance(result, dict):
            assert isinstance(parsed, WorkInput)
            result["want_list"] = parsed.work_id in runtime(request).library_prefs_store.load().want_list
        return SkillResult(
            ok=response.is_success
            and not (
                capability.name.startswith("intake.")
                and isinstance(result, dict)
                and result.get("action") == "error"
            ),
            capability=capability.name,
            status=response.status_code,
            result=result,
        )

    return router
