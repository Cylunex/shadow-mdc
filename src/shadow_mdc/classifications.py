"""Independent catalog classification dimensions and initial vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .tag_models import fold_tag

DimensionKey = Literal["contents", "types", "genres", "forms", "languages", "subtitle_languages", "regions"]


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


_SEED = ClassificationSeed.model_validate_json(
    (Path(__file__).parent / "data/classifications.seed.json").read_text(encoding="utf-8")
)
DIMENSIONS = _SEED.dimensions
DIMENSION_KEYS = tuple(dimension.id for dimension in DIMENSIONS)
DEFINITIONS = _SEED.terms
DEFINITION_ALIASES: dict[tuple[str, str], ClassificationDefinition] = {
    (term.dimension, fold_tag(value)): term
    for term in DEFINITIONS
    for value in (term.id, term.name, *term.aliases)
}
FAMILY_CONTENTS = {key: f"contents:{key}" for key in ("jav", "chinese", "western", "korean", "animation")}
