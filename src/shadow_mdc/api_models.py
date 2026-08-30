from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .domain import IdentityHints, OperationPlan, ProviderDescriptor, ProviderRecord
from .enums import MediaCategory, NfoPolicy, OutputMode
from .providers.base import ProviderFailure


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1)
    category: MediaCategory | None = None
    recursive: bool = True
    organize_template: str = "{studio}/{code_or_title}/{code_or_title}.{ext}"


class LibraryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    root_path: str
    category: MediaCategory
    recursive: bool
    organize_template: str
    created_at: datetime


class AssetOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    library_id: str
    work_id: str | None
    path: str
    size: int
    modified_ns: int
    duration_seconds: float | None
    oshash: str | None
    state: str
    hints: IdentityHints
    error: str | None
    created_at: datetime
    updated_at: datetime


class CandidateOut(BaseModel):
    id: str
    asset_id: str
    provider: str
    external_id: str
    score: float
    decision: str
    state: str
    record: ProviderRecord
    evidence: list[dict[str, object]]
    created_at: datetime


class IdentityOut(BaseModel):
    provider: str
    kind: str
    value: str
    source_url: str | None


class WorkOut(BaseModel):
    id: str
    title: str
    original_title: str | None
    primary_code: str | None
    family: str
    category: MediaCategory
    release_date: date | None
    runtime_seconds: int | None
    studio: str | None
    label: str | None
    series: str | None
    plot: str | None
    actors: list[str]
    directors: list[str]
    tags: list[str]
    artwork: list[dict[str, object]]
    field_sources: dict[str, str]
    identities: list[IdentityOut] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class IdentifyRequest(BaseModel):
    source_url: str | None = None
    title: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)


class ManualCandidateRequest(BaseModel):
    title: str | None = Field(default=None, min_length=1)
    studio: str | None = Field(default=None, min_length=1)
    series: str | None = Field(default=None, min_length=1)
    actors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    plot: str | None = None


class IdentifyOut(BaseModel):
    asset_id: str
    candidate_ids: tuple[str, ...]
    accepted_work_id: str | None
    failures: tuple[ProviderFailure, ...]


class WorkLookupRequest(BaseModel):
    code: str = Field(min_length=2, max_length=100)


class WorkLookupOut(BaseModel):
    work: WorkOut | None
    matched_records: int
    failures: tuple[ProviderFailure, ...]


class OrganizeRequest(BaseModel):
    mode: OutputMode = OutputMode.SIDECAR
    target_root: str | None = None
    template: str | None = None


class OrganizeApplyRequest(OrganizeRequest):
    token: str = Field(min_length=64, max_length=64)
    nfo_policy: NfoPolicy = NfoPolicy.ERROR


class BatchPlanOut(BaseModel):
    token: str
    asset_count: int
    operation_count: int
    conflict_count: int
    samples: tuple[OperationPlan, ...]
    truncated: bool


class BatchApplyOut(BaseModel):
    token: str
    attempted: int
    succeeded: int
    failed: int
    errors: tuple[str, ...]


class HealthOut(BaseModel):
    status: str = "ok"
    version: str


class ProviderListOut(BaseModel):
    providers: tuple[ProviderDescriptor, ...]


class ProviderDiagnoseRequest(BaseModel):
    code: str = Field(default="SONE-118", min_length=2, max_length=100)


class ProviderDiagnostic(BaseModel):
    provider: str
    status: str
    records: int = 0
    accepted: int = 0
    reason: str | None = None
    detail: str | None = None


class ProviderDiagnoseOut(BaseModel):
    code: str
    proxy_configured: bool
    retries: int
    diagnostics: tuple[ProviderDiagnostic, ...]


class ScanOut(BaseModel):
    discovered: int
    updated: int
    cataloged: int
    filtered: int
    skipped: int
    errors: tuple[str, ...]


class TaskRunOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    kind: str
    scope: str
    status: str
    summary: dict[str, object]
    error: str | None
    created_at: datetime
    finished_at: datetime | None


class FilterWordsPayload(BaseModel):
    words: tuple[str, ...] = Field(default_factory=tuple, max_length=5000)


class PlanOut(OperationPlan):
    pass
