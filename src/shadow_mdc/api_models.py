from datetime import date, datetime

from pydantic import BaseModel, ConfigDict, Field

from .domain import (
    IdentityHints,
    MediaTechnicalInfo,
    OperationPlan,
    ProviderDescriptor,
    ProviderRecord,
)
from .enums import MediaCategory, NfoPolicy, OutputMode, RecognitionScope
from .providers.base import ProviderFailure


class LibraryCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    root_path: str = Field(min_length=1)
    category: MediaCategory | None = None
    recursive: bool = True
    recognition_scope: RecognitionScope = RecognitionScope.ALL
    organize_template: str = "{group}/{subgroup}/{actor}/{folder_name}/{media_name}.{ext}"


class LibraryUpdate(BaseModel):
    recognition_scope: RecognitionScope


class LibraryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    name: str
    root_path: str
    category: MediaCategory
    recursive: bool
    recognition_scope: RecognitionScope
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
    media_info: MediaTechnicalInfo
    oshash: str | None
    state: str
    hints: IdentityHints
    error: str | None
    created_at: datetime
    updated_at: datetime


class AssetInboxHintsOut(BaseModel):
    family: str
    category: MediaCategory
    code: str | None
    title: str | None
    media_locator: str | None
    studio: str | None
    series: str | None
    actors: tuple[str, ...]


class AssetInboxMediaOut(BaseModel):
    video_codec: str | None
    audio_codec: str | None
    hdr_format: str | None
    quality_label: str | None


class AssetInboxOut(BaseModel):
    id: str
    library_id: str
    path: str
    state: str
    hints: AssetInboxHintsOut
    media_info: AssetInboxMediaOut


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


class ActorSummaryOut(BaseModel):
    id: str
    name: str
    image_url: str | None = None


class NonJavActorEdit(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=100)
    groups: tuple[str, ...] = Field(default_factory=tuple, max_length=50)
    categories: tuple[MediaCategory, ...] = (MediaCategory.OTHER,)
    biography: str | None = Field(default=None, max_length=5000)
    notes: str | None = Field(default=None, max_length=5000)


class NonJavActorWorkOut(BaseModel):
    id: str
    title: str
    code: str | None
    category: str
    studio: str | None = None
    series: str | None = None
    release_date: date | None = None
    image_url: str | None = None


class NonJavActorOut(BaseModel):
    name: str
    aliases: tuple[str, ...]
    groups: tuple[str, ...]
    categories: tuple[MediaCategory, ...]
    match_names: tuple[str, ...]
    image_url: str | None = None
    biography: str | None = None
    notes: str | None = None
    work_count: int = 0
    works: tuple[NonJavActorWorkOut, ...] = ()


class DirectoryActorAssignRequest(BaseModel):
    actor: str = Field(min_length=1, max_length=200)
    category: MediaCategory
    directory: str | None = Field(default=None, min_length=1, max_length=2000)


class DirectoryActorAssignOut(BaseModel):
    directory: str
    actor: str
    matched_assets: int
    cataloged: int
    skipped: int


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
    actor_entities: list[ActorSummaryOut] = Field(default_factory=list)
    directors: list[str]
    tags: list[str]
    artwork: list[dict[str, object]]
    image_url: str | None = None
    fanart_url: str | None = None
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


class BulkIdentifyRequest(BaseModel):
    limit: int = Field(default=20, ge=1, le=500)


class BulkIdentifyOut(BaseModel):
    queried_identities: int
    code_queries: int
    title_queries: int
    attempted_assets: int
    identified: int
    online_identified: int
    catalog_reused: int
    local_optimized: int
    unresolved: int
    provider_failures: int
    remaining_identities: int
    scope_skipped: int


class WorkLookupRequest(BaseModel):
    code: str = Field(min_length=2, max_length=100)


class WorkLookupOut(BaseModel):
    work: WorkOut | None
    matched_records: int
    failures: tuple[ProviderFailure, ...]


class BulkTranslateRequest(BaseModel):
    limit: int = Field(default=200, ge=1, le=1000)


class BulkTranslateOut(BaseModel):
    attempted: int
    translated: int
    skipped: int
    failed: int
    remaining: int
    errors: tuple[str, ...]


class ScreenshotGenerateOut(BaseModel):
    attempted: int
    generated: int
    skipped_strm: int
    skipped_cached: int
    skipped_untrusted: int
    failed: int
    errors: tuple[str, ...]


class ScreenshotGenerateRequest(BaseModel):
    limit: int = Field(default=50, ge=1, le=500)


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
    queued: int
    identified: int
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


class CatalogImportPathRequest(BaseModel):
    path: str = Field(min_length=1)
    dry_run: bool = False
    actors_only: bool = False
    works_only: bool = False
    include_formal: bool = True


class CatalogImportResultOut(BaseModel):
    dry_run: bool
    bundle_kind: str
    actors_added: int = 0
    actors_updated: int = 0
    actors_unchanged: int = 0
    actor_images_copied: int = 0
    works_created: int = 0
    works_updated: int = 0
    works_posters: int = 0
    works_actors_added: int = 0
    artwork_copied: int = 0
    formal_works_imported: int = 0
    jav_actors_merged: int = 0
    aliases_keys_added: int = 0
    filter_words_added: int = 0
    notes: tuple[str, ...] = ()

