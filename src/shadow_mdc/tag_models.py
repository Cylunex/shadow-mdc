"""Tag identities and names. IDs are permanent; names and aliases may evolve."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, unquote

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .runtime_data import data_directories, read_data, reference_path


def fold_tag(value: str) -> str:
    """Comparison form shared by registry aliases and unknown tag identities."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


@dataclass(frozen=True)
class TagReference:
    id: str
    name: str


@dataclass(frozen=True)
class TagDefinition(TagReference):
    aliases: tuple[str, ...] = ()
    hidden: bool = False
    revision: int = 1


class TagRegistry:
    """Immutable dictionary with explicit identities and reversible custom IDs."""

    def __init__(self, definitions: tuple[TagDefinition, ...], rules: TagRules | None = None) -> None:
        self.definitions = definitions
        self.rules = rules if rules is not None else TagRules()
        self.ids = {tag.id for tag in definitions}
        self._by_id: dict[str, TagDefinition] = {}
        self._by_alias: dict[str, TagDefinition] = {}
        for tag in definitions:
            if not re.fullmatch(r"tag:[a-z0-9]+(?:-[a-z0-9]+)*", tag.id) and not valid_raw_id(tag.id):
                raise ValueError(f"Invalid tag ID: {tag.id}")
            if tag.id in self._by_id or not tag.name.strip():
                raise ValueError(f"Duplicate ID or empty name: {tag.id}")
            self._by_id[tag.id] = tag
            for alias in (tag.name, *tag.aliases):
                key = fold_tag(alias)
                if not key or key.startswith(("tag:", "raw:")):
                    raise ValueError(f"Invalid alias for {tag.id}")
                previous = self._by_alias.get(key)
                if previous is not None and previous.id != tag.id:
                    raise ValueError(f"Ambiguous tag alias: {alias}")
                self._by_alias[key] = tag
        self.priority = {
            resolved.id: index
            for index, name in enumerate(self.rules.display_priority)
            if (resolved := self._by_alias.get(fold_tag(name))) is not None
        }
        self.noise_exact = frozenset(fold_tag(value) for value in self.rules.noise_exact)
        self.noise_prefixes = tuple(fold_tag(value) for value in self.rules.noise_prefixes)
        self._cached_resolve = lru_cache(maxsize=16_384)(self._resolve)

    def resolve(self, raw: str) -> TagDefinition | None:
        """Resolve an ID, name or alias; keep unknown tags in a separate namespace."""
        return self._cached_resolve(raw)

    def _resolve(self, raw: str) -> TagDefinition | None:
        text = unicodedata.normalize("NFKC", raw).strip()
        key = fold_tag(text)
        if text in self._by_id:
            return self._by_id[text]
        if key in self._by_id:
            return self._by_id[key]
        if key in self._by_alias:
            return self._by_alias[key]
        if key.startswith("tag:") or not key:
            return None
        if text.startswith("raw:"):
            try:
                decoded = unquote(text[4:], errors="strict")
            except UnicodeError:
                return None
            if not decoded or text != "raw:" + quote(fold_tag(decoded), safe=""):
                return None
            # A custom ID stays custom even if a new built-in alias is added later.
            return TagDefinition(id=text, name=decoded)
        return TagDefinition(id="raw:" + quote(key, safe=""), name=text, aliases=(text,))


class TagRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    noise_exact: list[str] = Field(default_factory=list, max_length=10000)
    noise_prefixes: list[str] = Field(default_factory=list, max_length=1000)
    display_priority: list[str] = Field(default_factory=list, max_length=10000)
    common_filter_tags: list[str] = Field(default_factory=list, max_length=10000)
    featured_labels: list[str] = Field(default_factory=list, max_length=10000)

    @field_validator("noise_exact", "noise_prefixes")
    @classmethod
    def valid_noise_rules(cls, values: list[str]) -> list[str]:
        if any(not value.strip() for value in values):
            raise ValueError("noise rules cannot be blank")
        return list(dict.fromkeys(fold_tag(value) for value in values))


class TagTerm(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    id: str
    name: str = Field(min_length=1, max_length=300)
    aliases: tuple[str, ...] = Field(default_factory=tuple, max_length=200)
    hidden: bool = False
    revision: int = Field(default=1, ge=1)

    def definition(self) -> TagDefinition:
        return TagDefinition(self.id, self.name, self.aliases, self.hidden, self.revision)


class TagDocument(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    terms: list[TagTerm] = Field(max_length=50000)
    rules: TagRules
    revision: int = Field(default=1, ge=1)

    def registry(self) -> TagRegistry:
        return TagRegistry(tuple(term.definition() for term in self.terms), self.rules)


def valid_raw_id(value: str) -> bool:
    try:
        decoded = unquote(value[4:], errors="strict")
    except UnicodeError:
        return False
    return value.startswith("raw:") and bool(decoded) and value == "raw:" + quote(fold_tag(decoded), safe="")


@lru_cache(maxsize=8)
def _parse_document(raw: bytes | None) -> TagDocument:
    return TagDocument(terms=[], rules=TagRules()) if raw is None else TagDocument.model_validate_json(raw)


def seed_document() -> TagDocument:
    """Read an optional external seed; a new library can start with an empty dictionary."""
    return _parse_document(read_data(reference_path("tag-dictionary.seed.json")))


@lru_cache(maxsize=8)
def _parse_registry(raw: bytes | None) -> TagRegistry:
    return _parse_document(raw).registry()


def default_registry() -> TagRegistry:
    """Use the selected library's persisted vocabulary outside an HTTP request as well."""
    raw = read_data(data_directories()[0] / "tag-dictionary.json")
    if raw is None:
        raw = read_data(reference_path("tag-dictionary.seed.json"))
    return _parse_registry(raw)
