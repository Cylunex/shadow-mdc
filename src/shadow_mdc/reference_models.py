"""Schemas for optional catalog vocabulary and maintenance datasets; no catalog records."""

from __future__ import annotations

from functools import lru_cache

from pydantic import BaseModel, ConfigDict, Field

from .runtime_data import read_data, reference_path


class CuratedActor(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    aliases: list[str] = Field(default_factory=list)
    groups: list[str] = Field(default_factory=list)
    categories: list[str] = Field(default_factory=list)
    biography: str | None = None


class CuratedWork(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    title: str
    studio: str | None = None
    series: str | None = None
    family: str
    category: str
    actors: list[str] = Field(default_factory=list)
    tags: list[str] = Field(default_factory=list)
    year: int | None = None
    code: str | None = None


class CatalogRules(BaseModel):
    model_config = ConfigDict(extra="forbid")
    platform_stems: tuple[str, ...] = Field(default_factory=tuple)
    studio_name_markers: tuple[str, ...] = Field(default_factory=tuple)
    platform_names: tuple[str, ...] = Field(default_factory=tuple)
    china_markers: tuple[str, ...] = Field(default_factory=tuple)
    japan_markers: tuple[str, ...] = Field(default_factory=tuple)
    korea_markers: tuple[str, ...] = Field(default_factory=tuple)
    europe_markers: tuple[str, ...] = Field(default_factory=tuple)
    animation_markers: tuple[str, ...] = Field(default_factory=tuple)
    other_markers: tuple[str, ...] = Field(default_factory=tuple)
    control_names: tuple[str, ...] = Field(default_factory=tuple)
    explicit_studio_labels: tuple[str, ...] = Field(default_factory=tuple)
    explicit_fuzzy_names: tuple[str, ...] = Field(default_factory=tuple)
    keep_borderline: tuple[str, ...] = Field(default_factory=tuple)
    prefix_series: tuple[str, ...] = Field(default_factory=tuple)
    suffix_series: tuple[str, ...] = Field(default_factory=tuple)
    studio_tokens: tuple[str, ...] = Field(default_factory=tuple)
    confident_merges: tuple[tuple[str, str], ...] = Field(default_factory=tuple)
    studio_site_hints: tuple[tuple[str, str], ...] = Field(default_factory=tuple)
    studio_site_extra: tuple[tuple[str, str], ...] = Field(default_factory=tuple)
    known_creator_sites: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    code_prefix_site: dict[str, str] = Field(default_factory=dict)
    archive_urls: tuple[str, ...] = Field(default_factory=tuple)
    blogger_sites: dict[str, tuple[str, ...]] = Field(default_factory=dict)
    blogger_names: dict[str, str] = Field(default_factory=dict)
    blogger_searches: tuple[tuple[str, str], ...] = Field(default_factory=tuple)
    chinese_sites: tuple[tuple[str, str, str], ...] = Field(default_factory=tuple)
    platform_seeds: tuple[CuratedWork, ...] = Field(default_factory=tuple)
    platform_actors: tuple[tuple[str, tuple[str, ...]], ...] = Field(default_factory=tuple)
    extra_actors: tuple[CuratedActor, ...] = Field(default_factory=tuple)
    china_extra: tuple[CuratedActor, ...] = Field(default_factory=tuple)
    extra_aliases: dict[str, tuple[str, ...]] = Field(default_factory=dict)


@lru_cache(maxsize=8)
def _parse_rules(raw: bytes | None) -> CatalogRules:
    return CatalogRules() if raw is None else CatalogRules.model_validate_json(raw)


def catalog_rules() -> CatalogRules:
    """Cache by document content, keeping directories and edits isolated."""
    return _parse_rules(read_data(reference_path("catalog-rules.json")))
