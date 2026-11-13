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

from .api_models import (
    ActorSubscriptionEdit,
    BulkIdentifyRequest,
    GfriendsFillRequest,
    IdentifyRequest,
    JavRankingSeedRequest,
    SaveMagnetsRequest,
    TaskResumeRequest,
    WorkLookupRequest,
    WorkUpdateRequest,
)
from .catalog_api import ClassificationTermCreate, ClassificationTermEdit, TaxonomyEdit
from .catalog_management import (
    CatalogFilter,
    OrganizationCreate,
    OrganizationEdit,
    OrganizationLink,
    PlaylistEdit,
    SeriesEdit,
    SeriesMember,
)
from .db.models import Work
from .intake_core import SubscribeRequest
from .services.catalog_people import MergeRequest, PersonEdit
from .services.discover import DiscoverList
from .skill_operations import ClassificationFill, FillInput, IntakeItem, LinkInput, RecoverInput, TagsEdit
from .tag_dictionary_api import RulesEdit, TagCreate, TagEdit, TagMerge

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


class OrganizationInput(EmptyInput):
    organization_id: str = Field(min_length=1, max_length=240)

    @field_validator("organization_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class SeriesInput(EmptyInput):
    series_id: str = Field(min_length=1, max_length=240)

    @field_validator("series_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class OrganizationCreateInput(OrganizationCreate, EmptyInput):
    pass


class OrganizationEditInput(OrganizationEdit, OrganizationInput):
    pass


class OrganizationLinkInput(OrganizationLink, OrganizationInput):
    pass


class SeriesCreateInput(SeriesEdit, EmptyInput):
    pass


class SeriesEditInput(SeriesEdit, SeriesInput):
    pass


class SeriesMemberInput(SeriesMember, SeriesInput):
    pass


class ActorsInput(EmptyInput):
    q: str = Field(default="", max_length=300)
    sort: Literal["works", "name"] = "works"
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=48, ge=1, le=100)
    followed: bool = False


class TagsInput(EmptyInput):
    only_with_works: bool = False


class TagDictionaryInput(EmptyInput):
    q: str = Field(default="", max_length=300)
    offset: int = Field(default=0, ge=0)
    limit: int = Field(default=100, ge=1, le=500)


class TagCreateInput(TagCreate, EmptyInput):
    pass


class TagEditInput(TagEdit, EmptyInput):
    tag_id: str = Field(min_length=1, max_length=4000)

    @field_validator("tag_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class TagRulesInput(RulesEdit, EmptyInput):
    pass


class ClassificationCreateInput(ClassificationTermCreate, EmptyInput):
    pass


class ClassificationEditInput(ClassificationTermEdit, EmptyInput):
    term_id: str = Field(min_length=1, max_length=240)
    revision: int = Field(ge=1)

    @field_validator("term_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class TasksInput(EmptyInput):
    limit: int = Field(default=30, ge=1, le=100)


class WantedInput(WorkInput):
    wanted: bool


class TaxonomyInput(TaxonomyEdit, WorkInput):
    pass


class WorkEditInput(WorkUpdateRequest, WorkInput):
    pass


class MagnetsInput(SaveMagnetsRequest, WorkInput):
    pass


class SubscriptionInput(ActorSubscriptionEdit, EmptyInput):
    pass


class SubscribeInput(SubscribeRequest, EmptyInput):
    pass


class ActorInput(EmptyInput):
    person_id: str = Field(min_length=1, max_length=240)

    @field_validator("person_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class ActorKeyInput(EmptyInput):
    actor_key: str = Field(min_length=1, max_length=240)

    @field_validator("actor_key")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class PlaylistCreateInput(PlaylistEdit, EmptyInput):
    pass


class PlaylistEditInput(PlaylistEdit, PlaylistInput):
    pass


class BrowseInput(EmptyInput):
    provider: str = Field(default="javdb", max_length=100)
    list: DiscoverList = "rankings_daily"
    page: int = Field(default=1, ge=1, le=1000)


class SearchInput(EmptyInput):
    provider: str = Field(default="javdb", max_length=100)
    q: str = Field(min_length=1, max_length=500)
    page: int = Field(default=1, ge=1, le=1000)


class MetadataInput(EmptyInput):
    code: str = Field(min_length=2, max_length=100)
    providers: bool | Annotated[str, Field(max_length=500)] | None = None

    @field_validator("code")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class GfriendsInput(GfriendsFillRequest, EmptyInput):
    pass


class PersonEditInput(PersonEdit, ActorInput):
    pass


class PersonMergeInput(MergeRequest, EmptyInput):
    pass


class PersonMergeUndoInput(EmptyInput):
    merge_id: str = Field(min_length=1, max_length=240)

    @field_validator("merge_id")
    @classmethod
    def safe_identifier(cls, value: str) -> str:
        return safe_id(value)


class RankingSeedInput(JavRankingSeedRequest, EmptyInput):
    pass


class ResumeInput(TaskResumeRequest, TaskInput):
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
    "organization_id",
    "series_id",
    "work_id",
    "task_id",
    "playlist_id",
    "code",
    "job_id",
    "library_id",
    "asset_id",
    "candidate_id",
    "person_id",
    "merge_id",
    "actor_key",
    "tag_id",
    "term_id",
]


@dataclass(frozen=True)
class Capability:
    name: str
    description: str
    effect: Literal["read", "write"]
    model: type[BaseModel]
    method: Literal["GET", "POST", "PUT", "PATCH", "DELETE"]
    path: str
    path_key: RouteKey | None = None


CAPABILITIES: tuple[Capability, ...] = (
    Capability("system.health", "检查 MDC 服务状态。", "read", EmptyInput, "GET", "/api/health"),
    Capability(
        "works.query",
        "按片名、演员、标签、分类、资料缺项和状态分页查询作品。",
        "read",
        CatalogFilter,
        "POST",
        "/api/catalog/works/query",
    ),
    Capability(
        "works.facets",
        "分别读取制作机构、厂牌、发布平台、作品系列及标签、分类和年份计数。",
        "read",
        CatalogFilter,
        "POST",
        "/api/catalog/facets",
    ),
    Capability(
        "works.metadata_gaps",
        "在当前筛选范围内按字段统计资料待补的作品数。",
        "read",
        CatalogFilter,
        "POST",
        "/api/catalog/metadata-gaps",
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
    Capability("organizations.list", "读取片商及所属工作室、厂牌、发布渠道。不含系列。",
               "read", EmptyInput, "GET", "/api/catalog/organizations"),
    Capability("organizations.create", "新增片商或所属机构。隶属关系须有明确依据。",
               "write", OrganizationCreateInput, "POST", "/api/catalog/organizations"),
    Capability("organizations.update", "带 revision 维护机构及其隶属关系。",
               "write", OrganizationEditInput, "PUT", "/api/catalog/organizations/{organization_id}",
               "organization_id"),
    Capability("organizations.link", "维护作品制作、发行、厂牌或平台署名。不设定系列成员。",
               "write", OrganizationLinkInput, "PUT", "/api/catalog/organizations/{organization_id}/works",
               "organization_id"),
    Capability("series.list", "读取独立系列档案、归属机构和成员计数。",
               "read", EmptyInput, "GET", "/api/catalog/series"),
    Capability("series.create", "创建作品系列。可指定已知归属机构。",
               "write", SeriesCreateInput, "POST", "/api/catalog/series"),
    Capability("series.update", "带 revision 维护系列资料与归属。",
               "write", SeriesEditInput, "PUT", "/api/catalog/series/{series_id}", "series_id"),
    Capability("series.add", "将明确的作品加入系列。不新增制作或发行署名。",
               "write", SeriesMemberInput, "PUT", "/api/catalog/series/{series_id}/works", "series_id"),
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
    Capability("tags.tree", "读取标签树的节点与父子关系。分组属于高层节点。",
               "read", EmptyInput, "GET", "/api/catalog/tags/tree"),
    Capability(
        "classifications.list",
        "读取作品分类的正式 ID、名称及计数。题材通过标签维护。",
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
CAPABILITIES += (
    Capability(
        "tags.dictionary",
        "分页读取可管理的标签词条、别名、隐藏状态和修订号。",
        "read",
        TagDictionaryInput,
        "GET",
        "/api/catalog/tags",
    ),
    Capability("tags.create", "新增持久化标签词条。", "write", TagCreateInput, "POST", "/api/catalog/tags"),
    Capability("tags.merge", "合并明确同义的词条,保留旧 ID 解析和作品原始标签。",
               "write", TagMerge, "POST", "/api/catalog/tags/merge"),
    Capability(
        "tags.update",
        "修改标签名、别名或隐藏状态,保持 ID。携带当前 revision。",
        "write",
        TagEditInput,
        "PUT",
        "/api/catalog/tags",
    ),
    Capability(
        "tags.rules", "读取持久化的标签展示和噪声规则。", "read", EmptyInput, "GET", "/api/catalog/tags/rules"
    ),
    Capability(
        "tags.rules.update",
        "按明确要求修改展示和噪声规则,携带字典 revision。",
        "write",
        TagRulesInput,
        "PUT",
        "/api/catalog/tags/rules",
    ),
    Capability(
        "classifications.create",
        "在指定作品分类中新增正式词条。",
        "write",
        ClassificationCreateInput,
        "POST",
        "/api/catalog/classifications/terms",
    ),
    Capability(
        "classifications.update",
        "修改分类词条名与别名,保留 ID。携带当前 revision。",
        "write",
        ClassificationEditInput,
        "PUT",
        "/api/catalog/classifications/terms/{term_id}",
        "term_id",
    ),
    Capability(
        "intake.health",
        "检查离线后端连接、目标目录与元数据回退状态。",
        "read",
        EmptyInput,
        "GET",
        "/api/intake/v1/health",
    ),
    Capability(
        "intake.link.submit",
        "给已有作品提交下载链接。ed2k 仅限支持它的 OpenList 后端。",
        "write",
        LinkInput,
        "POST",
        "/api/operator/v1/link",
    ),
    Capability(
        "works.update",
        "修改展示名、演员、剧情和标签。默认锁定手改字段。",
        "write",
        WorkEditInput,
        "PATCH",
        "/api/works/{work_id}",
        "work_id",
    ),
    Capability(
        "works.tags",
        "增删指定标签并保留其它标签。支持别名归一。",
        "write",
        TagsEdit,
        "POST",
        "/api/operator/v1/tags",
    ),
    Capability(
        "works.fill",
        "只补指定的空元数据、缺译或缺图。保护已有值与字段锁。",
        "write",
        FillInput,
        "POST",
        "/api/operator/v1/fill",
    ),
    Capability(
        "works.magnets",
        "读取作品已有磁力和质量线索。",
        "read",
        WorkInput,
        "GET",
        "/api/works/{work_id}/magnets",
        "work_id",
    ),
    Capability(
        "works.magnets.save",
        "保存明确给出的磁力,不提交下载。",
        "write",
        MagnetsInput,
        "POST",
        "/api/works/{work_id}/magnets",
        "work_id",
    ),
    Capability(
        "classifications.fill",
        "仅回填待补维度。已有明确分类保持原值。",
        "write",
        ClassificationFill,
        "POST",
        "/api/operator/v1/classifications/fill",
    ),
    Capability(
        "intake.item",
        "处理批量清单中的单项:建档或离线入库并追加标签。",
        "write",
        IntakeItem,
        "POST",
        "/api/operator/v1/intake",
    ),
    Capability(
        "intake.recover",
        "重新核对既有离线结果并补导出,不新提交下载。",
        "write",
        RecoverInput,
        "POST",
        "/api/operator/v1/recover",
    ),
    Capability(
        "intake.next",
        "按明确要求切换失败或运行任务的下一磁力。",
        "write",
        TaskInput,
        "POST",
        "/api/pan/offline/tasks/{task_id}/next",
        "task_id",
    ),
    Capability(
        "intake.cancel",
        "按明确要求取消离线任务。",
        "write",
        TaskInput,
        "POST",
        "/api/pan/offline/tasks/{task_id}/cancel",
        "task_id",
    ),
    Capability(
        "intake.pipeline",
        "读取 115/OpenList、STRM、NFO 与 Emby 收口状态。",
        "read",
        EmptyInput,
        "GET",
        "/api/pan/pipeline",
    ),
    Capability(
        "metadata.lookup",
        "只读查询番号元数据,可选来源。",
        "read",
        MetadataInput,
        "GET",
        "/api/intake/v1/metadata/{code}",
        "code",
    ),
    Capability(
        "playback.code",
        "按番号解析 Emby 及已配置外站播放源。",
        "read",
        CodeInput,
        "GET",
        "/api/intake/v1/play/{code}",
        "code",
    ),
    Capability(
        "playback.work",
        "按作品 ID 解析现有播放源。",
        "read",
        WorkInput,
        "GET",
        "/api/works/{work_id}/play",
        "work_id",
    ),
    Capability(
        "subscriptions.add",
        "按番号或演员名新增盯磁,二者只传一个。",
        "write",
        SubscribeInput,
        "POST",
        "/api/intake/v1/subscribe",
    ),
    Capability(
        "subscriptions.list",
        "读取想看、演员订阅和发现队列。",
        "read",
        EmptyInput,
        "GET",
        "/api/library-prefs",
    ),
    Capability(
        "subscriptions.set",
        "设定演员盯磁、起始日期与人数限制。",
        "write",
        SubscriptionInput,
        "PUT",
        "/api/library-prefs/subscriptions",
    ),
    Capability(
        "subscriptions.remove",
        "按明确要求取消演员订阅。",
        "write",
        ActorKeyInput,
        "DELETE",
        "/api/library-prefs/subscriptions/{actor_key}",
        "actor_key",
    ),
    Capability(
        "subscriptions.scan",
        "扫描订阅演员的新作品。",
        "write",
        EmptyInput,
        "POST",
        "/api/library-prefs/subscriptions/scan",
    ),
    Capability(
        "subscriptions.status",
        "读取盯磁最近运行与错误。",
        "read",
        EmptyInput,
        "GET",
        "/api/pan/subscription-watch/status",
    ),
    Capability(
        "subscriptions.run",
        "执行一次既有盯磁策略,可能自动提交离线。",
        "write",
        EmptyInput,
        "POST",
        "/api/pan/subscription-watch/run",
    ),
    Capability(
        "actors.detail",
        "读取统一演员档案和作品关联。",
        "read",
        ActorInput,
        "GET",
        "/api/catalog/people/{person_id}",
        "person_id",
    ),
    Capability(
        "actors.images.fill",
        "沿用头像库补演员图片,默认不强制刷新。",
        "write",
        GfriendsInput,
        "POST",
        "/api/actors/fill-gfriends-images",
    ),
    Capability(
        "actors.update", "带 revision 更新统一演员档案,省略字段保持原值。", "write",
        PersonEditInput, "PUT", "/api/catalog/people/{person_id}", "person_id",
    ),
    Capability(
        "actors.merge", "带双方 revision 可撤销合并已确认的同一演员。", "write",
        PersonMergeInput, "POST", "/api/catalog/people-merge",
    ),
    Capability(
        "actors.merges", "读取最近演员合并及撤销状态。", "read",
        EmptyInput, "GET", "/api/catalog/people-merges",
    ),
    Capability(
        "actors.merge.undo", "撤销未被后续编辑改变的演员合并。", "write",
        PersonMergeUndoInput, "POST", "/api/catalog/people-merges/{merge_id}/undo", "merge_id",
    ),
    Capability(
        "playlists.create",
        "创建人工或动态片单。",
        "write",
        PlaylistCreateInput,
        "POST",
        "/api/catalog/playlists",
    ),
    Capability(
        "playlists.update",
        "带 revision 更新片单,冲突后先回读。",
        "write",
        PlaylistEditInput,
        "PUT",
        "/api/catalog/playlists/{playlist_id}",
        "playlist_id",
    ),
    Capability(
        "discover.browse",
        "只读浏览日榜和来源片单。尚未入库。",
        "read",
        BrowseInput,
        "GET",
        "/api/discover/browse",
    ),
    Capability("discover.search", "只读搜索来源作品。", "read", SearchInput, "GET", "/api/discover/search"),
    Capability(
        "rankings.seed",
        "从受支持榜单批量建档,返回新建、跳过和失败明细。",
        "write",
        RankingSeedInput,
        "POST",
        "/api/javranking/seed",
    ),
    Capability(
        "strm.status", "读取 STRM 维护、配置和通知结果。", "read", EmptyInput, "GET", "/api/pan/strm/status"
    ),
    Capability(
        "strm.rewrite", "按当前配置重写已管理 STRM。", "write", EmptyInput, "POST", "/api/pan/strm/rewrite"
    ),
    Capability(
        "strm.notify", "重试已有 Emby 待通知路径。", "write", EmptyInput, "POST", "/api/pan/strm/notify/retry"
    ),
    Capability(
        "tasks.retry",
        "重试支持重试的失败任务。",
        "write",
        TaskInput,
        "POST",
        "/api/tasks/{task_id}/retry",
        "task_id",
    ),
    Capability(
        "tasks.resume",
        "恢复服务支持的持久任务。",
        "write",
        ResumeInput,
        "POST",
        "/api/tasks/{task_id}/resume",
        "task_id",
    ),
    Capability(
        "tasks.cancel",
        "按明确要求取消通用任务。",
        "write",
        TaskInput,
        "POST",
        "/api/tasks/{task_id}/cancel",
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
        values = parsed.model_dump(
            mode="json",
            exclude_unset=isinstance(
                parsed,
                (TaxonomyInput, WorkEditInput, ClassificationFill, TagEditInput,
                 ClassificationEditInput, PersonEditInput),
            ),
        )
        path = capability.path
        if capability.path_key:
            identity = values.pop(capability.path_key)
            path = path.format(**{capability.path_key: quote(str(identity), safe="")})
        headers = {"X-Catalog-Owner": owner}
        if path.startswith(("/api/intake/v1/", "/api/operator/v1/")):
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
        result = _JSON.validate_python(response.json()) if response.content else None
        if capability.name == "works.detail" and response.is_success and isinstance(result, dict):
            assert isinstance(parsed, WorkInput)
            result["want_list"] = parsed.work_id in runtime(request).library_prefs_store.load().want_list
        return SkillResult(
            ok=response.is_success
            and not (
                isinstance(result, dict)
                and (
                    result.get("action") == "error"
                    or result.get("outcome") == "failed"
                    or (capability.name == "works.lookup" and result.get("work") is None)
                    or (capability.name == "works.refresh" and result.get("accepted_work_id") is None)
                    or (capability.name == "rankings.seed" and bool(result.get("failure_count")))
                )
            ),
            capability=capability.name,
            status=response.status_code,
            result=result,
        )

    return router
