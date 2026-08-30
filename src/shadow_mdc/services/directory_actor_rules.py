import os
import unicodedata
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..enums import MediaCategory


class DirectoryActorRule(BaseModel):
    """A user-confirmed performer identity scoped to one media directory."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    directory: str
    actor: str
    category: MediaCategory

    @field_validator("directory", "actor")
    @classmethod
    def validate_text(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("directory actor rule values must not be blank")
        return cleaned


class DirectoryActorRules(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    rules: tuple[DirectoryActorRule, ...] = ()

    def match(self, path: Path, root: Path) -> DirectoryActorRule | None:
        """Return the nearest explicit rule without walking above the library root."""

        root_key = normalize_directory(root)
        current = path.parent
        while True:
            current_key = normalize_directory(current)
            for rule in self.rules:
                if normalize_directory(Path(rule.directory)) == current_key:
                    return rule
            if current_key == root_key or current.parent == current:
                return None
            current = current.parent


class DirectoryActorRuleStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> DirectoryActorRules:
        if not self._path.is_file():
            return DirectoryActorRules()
        try:
            return DirectoryActorRules.model_validate_json(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"cannot read directory actor rules: {exc}") from exc

    def save(self, rules: DirectoryActorRules) -> DirectoryActorRules:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(rules.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        return rules

    def upsert(
        self,
        rule: DirectoryActorRule,
        *,
        replace_descendants: bool = False,
    ) -> DirectoryActorRules:
        key = normalize_directory(Path(rule.directory))
        retained = [
            existing
            for existing in self.load().rules
            if normalize_directory(Path(existing.directory)) != key
            and not (
                replace_descendants
                and Path(existing.directory).resolve(strict=False).is_relative_to(
                    Path(rule.directory).resolve(strict=False)
                )
            )
        ]
        retained.append(rule)
        retained.sort(key=lambda item: normalize_directory(Path(item.directory)))
        return self.save(DirectoryActorRules(rules=tuple(retained)))

    def rename_actor(self, old_name: str, new_name: str) -> DirectoryActorRules:
        normalized_old = _normalize_name(old_name)
        current = self.load()
        updated = tuple(
            rule.model_copy(update={"actor": new_name})
            if _normalize_name(rule.actor) == normalized_old
            else rule
            for rule in current.rules
        )
        return self.save(current.model_copy(update={"rules": updated}))


def normalize_directory(path: Path) -> str:
    return os.path.normcase(str(path.resolve(strict=False))).casefold()


def _normalize_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()
