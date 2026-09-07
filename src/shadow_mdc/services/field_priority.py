"""Per-field default source priority and optional global locks."""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

DEFAULT_FIELD_PRIORITIES: dict[str, list[str]] = {
    "title": ["local-manual", "translation:deepl", "translation:deeplx", "translation:custom", "translation:google", "javdb", "javbus", "r18dev", "fanza", "theporndb", "local-path"],
    "plot": ["local-manual", "translation:deepl", "translation:deeplx", "translation:custom", "translation:google", "javdb", "r18dev", "theporndb", "javbus"],
    "actors": ["local-manual", "r18dev", "fanza", "javlibrary", "javdb", "javbus", "theporndb", "local-path"],
    "studio": ["local-manual", "r18dev", "fanza", "javdb", "javbus", "local-path"],
    "series": ["local-manual", "javdb", "r18dev", "javbus", "local-path"],
    "tags": ["local-manual", "javdb", "javbus", "theporndb", "local-path"],
    "release_date": ["local-manual", "r18dev", "fanza", "javdb", "javbus", "theporndb"],
    "runtime_seconds": ["local-manual", "r18dev", "fanza", "javdb", "theporndb"],
}

CONFIGURABLE_FIELDS = tuple(DEFAULT_FIELD_PRIORITIES.keys())


class FieldPriorityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    priorities: dict[str, list[str]] = Field(default_factory=lambda: {k: list(v) for k, v in DEFAULT_FIELD_PRIORITIES.items()})
    default_locks: list[str] = Field(default_factory=list)


class FieldPriorityStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> FieldPriorityConfig:
        if not self._path.is_file():
            return FieldPriorityConfig()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8-sig"))
            return FieldPriorityConfig.model_validate(payload)
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read field priority config: {exc}") from exc

    def save(self, config: FieldPriorityConfig) -> FieldPriorityConfig:
        cleaned = FieldPriorityConfig(
            priorities={
                field: list(dict.fromkeys(sources))
                for field, sources in config.priorities.items()
                if field in DEFAULT_FIELD_PRIORITIES
            },
            default_locks=[item for item in config.default_locks if item in DEFAULT_FIELD_PRIORITIES],
        )
        # Fill missing fields with defaults.
        priorities = {k: list(v) for k, v in DEFAULT_FIELD_PRIORITIES.items()}
        priorities.update(cleaned.priorities)
        final = FieldPriorityConfig(priorities=priorities, default_locks=list(cleaned.default_locks))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(final.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        return final


def source_rank(config: FieldPriorityConfig, field: str, source: str) -> int:
    order = config.priorities.get(field) or DEFAULT_FIELD_PRIORITIES.get(field) or []
    try:
        return order.index(source)
    except ValueError:
        return len(order) + 50
