"""Independent catalog classification dimensions and initial vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import ContentFamily
from .runtime_data import read_data, reference_path

DimensionKey = Literal["contents", "types", "forms", "languages", "regions"]
DIMENSION_KEYS: tuple[DimensionKey, ...] = (
    "contents",
    "types",
    "forms",
    "languages",
    "regions",
)
LEGACY_DIMENSIONS = ("genres", "subtitle_languages")
# These permanent IDs belong to the retired manual duration classification.
DURATION_FORM_IDS = frozenset({"forms:feature", "forms:short"})
FAMILY_CONTENTS = {
    family.value: f"contents:{family.value}"
    for family in ContentFamily
    if family is not ContentFamily.UNKNOWN
}


@dataclass(frozen=True)
class ClassificationDimension:
    id: DimensionKey
    name: str
    description: str


@dataclass(frozen=True)
class StoredClassificationDimension:
    """External seeds can still contain retired dimensions for migration."""

    id: str
    name: str
    description: str


class ClassificationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contents: list[str] = Field(default_factory=list, max_length=100)
    types: list[str] = Field(default_factory=list, max_length=100)
    forms: list[str] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=list, max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=100)

    def selections(self) -> dict[DimensionKey, list[str]]:
        """Typed dimension values without reflection or dynamic model attributes."""
        return {
            "contents": self.contents,
            "types": self.types,
            "forms": self.forms,
            "languages": self.languages,
            "regions": self.regions,
        }


@dataclass(frozen=True)
class ClassificationDefinition:
    id: str
    dimension: str
    name: str
    aliases: tuple[str, ...] = ()


class ClassificationSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensions: tuple[StoredClassificationDimension, ...]
    terms: tuple[ClassificationDefinition, ...]

    @model_validator(mode="after")
    def validate_structure(self) -> ClassificationSeed:
        keys = [dimension.id for dimension in self.dimensions]
        if (
            len(keys) != len(set(keys))
            or not set(DIMENSION_KEYS).issubset(keys)
            or set(keys) - set((*DIMENSION_KEYS, *LEGACY_DIMENSIONS))
        ):
            raise ValueError("classification data must describe the supported dimensions")
        if len({term.id for term in self.terms}) != len(self.terms):
            raise ValueError("duplicate classification term ID")
        if any(not term.id.startswith(f"{term.dimension}:") for term in self.terms):
            raise ValueError("classification ID belongs to a different dimension")
        return self


@lru_cache(maxsize=8)
def _parse_seed(raw: bytes | None) -> ClassificationSeed:
    if raw is None:
        return ClassificationSeed(
            dimensions=tuple(StoredClassificationDimension(key, key, "") for key in DIMENSION_KEYS),
            terms=(),
        )
    return ClassificationSeed.model_validate_json(raw)


def classification_seed() -> ClassificationSeed:
    """Optional vocabulary is external; dimension IDs remain part of the API schema."""
    return _parse_seed(read_data(reference_path("classifications.seed.json")))


def classification_dimensions() -> tuple[ClassificationDimension, ...]:
    return tuple(
        ClassificationDimension(
            dimension.id,
            {"contents": "内容分区", "languages": "语言"}.get(dimension.id, dimension.name),
            "作品原始对白语言。配音与字幕属于具体片源版本。"
            if dimension.id == "languages"
            else dimension.description,
        )
        for dimension in classification_seed().dimensions
        if dimension.id in DIMENSION_KEYS
    )


def classification_definitions() -> tuple[ClassificationDefinition, ...]:
    """Include the structural sentinels required for unclassified works and family projections."""
    terms = classification_seed().terms
    identities = {term.id for term in terms}
    required = [ClassificationDefinition(f"{key}:unknown", key, "待补") for key in DIMENSION_KEYS]
    required.extend(
        ClassificationDefinition(identity, "contents", key) for key, identity in FAMILY_CONTENTS.items()
    )
    return (*terms, *(term for term in required if term.id not in identities))
