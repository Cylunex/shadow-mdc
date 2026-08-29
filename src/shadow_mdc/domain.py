from datetime import date
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from .enums import (
    ContentFamily,
    MatchDecision,
    MediaCategory,
    OperationKind,
    ProviderRequirement,
    QueryMode,
)


class IdentityHints(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    term: str = Field(min_length=1)
    mode: QueryMode
    family: ContentFamily = ContentFamily.UNKNOWN
    category: MediaCategory = MediaCategory.OTHER
    code: str | None = None
    title: str | None = None
    source_url: str | None = None
    external_ids: dict[str, str] = Field(default_factory=dict)
    fingerprints: dict[str, str] = Field(default_factory=dict)
    duration_seconds: float | None = Field(default=None, ge=0)
    file_path: str | None = None
    media_locator: str | None = None
    studio: str | None = None
    series: str | None = None
    actors: tuple[str, ...] = ()
    alias_evidence: tuple[str, ...] = ()

    @model_validator(mode="after")
    def validate_primary_hint(self) -> "IdentityHints":
        if self.mode is QueryMode.CODE and not self.code:
            raise ValueError("code mode requires code")
        if self.mode in {QueryMode.URL, QueryMode.CATALOGUE} and not self.source_url:
            raise ValueError(f"{self.mode.value} mode requires source_url")
        if self.mode is QueryMode.FINGERPRINT and not self.fingerprints:
            raise ValueError("fingerprint mode requires fingerprints")
        if self.mode is QueryMode.EXTERNAL_ID and not self.external_ids:
            raise ValueError("external_id mode requires external_ids")
        return self


class Artwork(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    url: HttpUrl
    kind: str
    width: int | None = Field(default=None, ge=1)
    height: int | None = Field(default=None, ge=1)


class ProviderRecord(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1)
    external_id: str = Field(min_length=1)
    source_url: str | None = None
    code: str | None = None
    title: str = Field(min_length=1)
    original_title: str | None = None
    family: ContentFamily = ContentFamily.UNKNOWN
    category: MediaCategory = MediaCategory.OTHER
    release_date: date | None = None
    runtime_seconds: int | None = Field(default=None, ge=0)
    studio: str | None = None
    label: str | None = None
    series: str | None = None
    plot: str | None = None
    actors: tuple[str, ...] = ()
    directors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    artwork: tuple[Artwork, ...] = ()
    fingerprints: dict[str, str] = Field(default_factory=dict)
    language: str | None = None


class MatchEvidence(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: str
    contribution: float = Field(ge=0, le=1)
    detail: str


class ScoredCandidate(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    record: ProviderRecord
    score: float = Field(ge=0, le=1)
    decision: MatchDecision
    evidence: tuple[MatchEvidence, ...]


class ProviderDescriptor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    name: str
    query_modes: frozenset[QueryMode]
    families: frozenset[ContentFamily]
    requirements: frozenset[ProviderRequirement] = frozenset()
    configured: bool = True


class FileOperation(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    kind: OperationKind
    source: str | None = None
    destination: str
    conflict: bool = False
    detail: str | None = None


class OperationPlan(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    token: str
    operations: tuple[FileOperation, ...]

    @property
    def has_conflicts(self) -> bool:
        return any(operation.conflict for operation in self.operations)


def file_name(path: str) -> str:
    return Path(path).name
