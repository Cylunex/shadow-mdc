"""Independent catalog classification dimensions and initial vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from .enums import ContentFamily
from .runtime_data import read_data, reference_path

DimensionKey = Literal["contents", "types", "genres", "forms", "languages", "subtitle_languages", "regions"]
DIMENSION_KEYS: tuple[DimensionKey, ...] = (
    "contents",
    "types",
    "genres",
    "forms",
    "languages",
    "subtitle_languages",
    "regions",
)
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


class ClassificationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contents: list[str] = Field(default_factory=list, max_length=100)
    types: list[str] = Field(default_factory=list, max_length=100)
    genres: list[str] = Field(default_factory=list, max_length=100)
    forms: list[str] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=list, max_length=100)
    subtitle_languages: list[str] = Field(default_factory=list, max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=100)

    def selections(self) -> dict[DimensionKey, list[str]]:
        """Typed dimension values without reflection or dynamic model attributes."""
        return {
            "contents": self.contents,
            "types": self.types,
            "genres": self.genres,
            "forms": self.forms,
            "languages": self.languages,
            "subtitle_languages": self.subtitle_languages,
            "regions": self.regions,
        }


@dataclass(frozen=True)
class ClassificationDefinition:
    id: str
    dimension: DimensionKey
    name: str
    aliases: tuple[str, ...] = ()


class ClassificationSeed(BaseModel):
    model_config = ConfigDict(extra="forbid")
    dimensions: tuple[ClassificationDimension, ...]
    terms: tuple[ClassificationDefinition, ...]

    @model_validator(mode="after")
    def validate_structure(self) -> ClassificationSeed:
        if len(self.dimensions) != len(DIMENSION_KEYS) or {d.id for d in self.dimensions} != set(
            DIMENSION_KEYS
        ):
            raise ValueError("classification data must describe the seven supported dimensions")
        if len({term.id for term in self.terms}) != len(self.terms):
            raise ValueError("duplicate classification term ID")
        if any(not term.id.startswith(f"{term.dimension}:") for term in self.terms):
            raise ValueError("classification ID belongs to a different dimension")
        return self


@lru_cache(maxsize=8)
def _parse_seed(raw: bytes | None) -> ClassificationSeed:
    if raw is None:
        return ClassificationSeed(
            dimensions=tuple(ClassificationDimension(key, key, "") for key in DIMENSION_KEYS),
            terms=(),
        )
    return ClassificationSeed.model_validate_json(raw)


def classification_seed() -> ClassificationSeed:
    """Optional vocabulary is external; dimension IDs remain part of the API schema."""
    return _parse_seed(read_data(reference_path("classifications.seed.json")))


def classification_dimensions() -> tuple[ClassificationDimension, ...]:
    return classification_seed().dimensions


def classification_definitions() -> tuple[ClassificationDefinition, ...]:
    """Include the structural sentinels required for unclassified works and family projections."""
    terms = classification_seed().terms
    identities = {term.id for term in terms}
    required = [ClassificationDefinition(f"{key}:unknown", key, "待补") for key in DIMENSION_KEYS]
    required.extend(
        ClassificationDefinition(identity, "contents", key) for key, identity in FAMILY_CONTENTS.items()
    )
    return (*terms, *(term for term in required if term.id not in identities))
