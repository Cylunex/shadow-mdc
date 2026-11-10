"""Authenticated, allowlisted HTTP capabilities for the MDC operator skill."""

from __future__ import annotations

import hmac
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING, Annotated, Literal
from urllib.parse import quote

import httpx
from fastapi import APIRouter, Depends, Header, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field, JsonValue, TypeAdapter, ValidationError, field_validator

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


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    effect: Literal["read", "write"]
    model: type[BaseModel]
    method: Literal["GET", "POST", "PUT"]
    path: str
    path_key: Literal["work_id", "task_id", "playlist_id"] | None = None


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
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=request.app), base_url="http://mdc"
        ) as client:
            response = await client.request(
                capability.method,
                path,
                headers=headers,
                params=values if capability.method == "GET" else None,
                json=values if capability.method != "GET" else None,
            )
        result = _JSON.validate_python(response.json())
        if capability.name == "works.detail" and response.is_success and isinstance(result, dict):
            assert isinstance(parsed, WorkInput)
            result["want_list"] = parsed.work_id in runtime(request).library_prefs_store.load().want_list
        return SkillResult(
            ok=response.is_success,
            capability=capability.name,
            status=response.status_code,
            result=result,
        )

    return router
