import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator

from ..enums import MediaCategory
from ..identity import IdentityAliasRules

_PARENTHETICAL = re.compile(r"\s*\([^)]*\)\s*$")
_HANDLE_SUFFIX = re.compile(r"^(.+?)\s+@([A-Za-z0-9_]{2,32})$")
_MARKDOWN = re.compile(r"^(?:#{1,6}\s*|\*\*|---)")
_GENERIC_NAMES = frozenset(
    {
        "cx系列女优",
        "fun vip",
        "麻豆12金钗系列",
        "杏吧热门主播",
        "性吧论坛主",
        "杏吧发帖达人",
        "杏吧图吧聊天",
        "酒后强奸女儿",
    }
)
_AMBIGUOUS_ASCII_NAMES = frozenset(
    {
        "ari",
        "fa",
        "gabby",
        "genesis",
        "hannah",
        "luna",
        "max",
        "mia",
        "nikki",
        "rachel",
        "sky",
        "summer",
        "tina",
        "vera",
        "yvette",
    }
)


class NonJavActorProfile(BaseModel):
    """Curated non-JAV performer or creator identity used only as local path evidence."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str
    aliases: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    categories: tuple[MediaCategory, ...] = ()
    match_names: tuple[str, ...] = ()
    image_file: str | None = None
    biography: str | None = None
    notes: str | None = None

    @field_validator("name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        cleaned = value.strip()
        if not cleaned:
            raise ValueError("actor name must not be blank")
        return cleaned


class NonJavActorCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    source: str = "user-curated"
    actors: tuple[NonJavActorProfile, ...] = ()


class NonJavActorCatalogStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> NonJavActorCatalog:
        if not self._path.is_file():
            return NonJavActorCatalog()
        try:
            return NonJavActorCatalog.model_validate_json(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"cannot read non-JAV actor catalog: {exc}") from exc

    def save(self, catalog: NonJavActorCatalog) -> NonJavActorCatalog:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(catalog.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        return catalog

    def upsert(
        self,
        profile: NonJavActorProfile,
        *,
        previous_name: str | None = None,
    ) -> NonJavActorCatalog:
        current = self.load()
        replaced_key = _normalize(previous_name or profile.name)
        duplicate_key = _normalize(profile.name)
        retained = [
            actor for actor in current.actors if _normalize(actor.name) not in {replaced_key, duplicate_key}
        ]
        retained.append(profile)
        retained.sort(key=lambda actor: actor.name.casefold())
        return self.save(current.model_copy(update={"actors": tuple(retained)}))

    def delete(self, name: str) -> NonJavActorCatalog:
        current = self.load()
        key = _normalize(name)
        retained = tuple(actor for actor in current.actors if _normalize(actor.name) != key)
        if len(retained) == len(current.actors):
            raise LookupError(f"non-JAV actor {name} not found")
        return self.save(current.model_copy(update={"actors": retained}))

    def get(self, name: str) -> NonJavActorProfile | None:
        key = _normalize(name)
        return next((actor for actor in self.load().actors if _normalize(actor.name) == key), None)


@dataclass
class _ActorAccumulator:
    name: str
    aliases: set[str] = field(default_factory=set)
    groups: set[str] = field(default_factory=set)
    categories: set[MediaCategory] = field(default_factory=set)


def parse_non_jav_actor_text(text: str, *, source: str) -> NonJavActorCatalog:
    """Normalize the user's loose Markdown/CSV list into a deterministic catalog."""

    accumulators: dict[str, _ActorAccumulator] = {}
    group = "uncategorized"
    category = MediaCategory.OTHER
    for raw_line in text.splitlines():
        line = unicodedata.normalize("NFKC", raw_line).strip()
        if detected := _heading_metadata(line):
            group, category = detected
            continue
        if not line or "," not in line or _MARKDOWN.match(line) or line.startswith("("):
            continue
        fields = tuple(value.strip() for value in line.split(",") if value.strip())
        if len(fields) < 2:
            continue
        canonical, aliases = _parse_actor_fields(fields)
        if not canonical or _is_generic(canonical):
            continue
        key = _normalize(canonical)
        accumulator = accumulators.setdefault(key, _ActorAccumulator(name=canonical))
        accumulator.aliases.update(
            alias for alias in aliases if not _is_generic(alias) and _normalize(alias) != key
        )
        accumulator.groups.add(group)
        accumulator.categories.add(category)

    profiles = tuple(
        NonJavActorProfile(
            name=item.name,
            aliases=tuple(sorted(item.aliases, key=str.casefold)),
            groups=tuple(sorted(item.groups)),
            categories=tuple(sorted(item.categories, key=lambda value: value.value)),
            match_names=tuple(
                name
                for name in (item.name, *sorted(item.aliases, key=str.casefold))
                if _is_safe_match_name(name)
            ),
        )
        for item in sorted(accumulators.values(), key=lambda value: value.name.casefold())
    )
    return NonJavActorCatalog(source=source, actors=profiles)


def enrich_non_jav_actor_aliases(
    rules: IdentityAliasRules,
    catalog: NonJavActorCatalog,
) -> IdentityAliasRules:
    """Add curated non-JAV names to scan hints without changing the JAV actor catalog."""

    actors = dict(rules.actors)
    for profile in catalog.actors:
        for match_name in profile.match_names:
            actors.setdefault(match_name, profile.name)
    return rules.model_copy(update={"actors": actors})


def build_non_jav_actor_profile(
    *,
    name: str,
    aliases: tuple[str, ...],
    groups: tuple[str, ...],
    categories: tuple[MediaCategory, ...],
    image_file: str | None = None,
    biography: str | None = None,
    notes: str | None = None,
) -> NonJavActorProfile:
    """Normalize an actor edited in the UI using the same matching safety rules as imports."""

    cleaned_name = " ".join(unicodedata.normalize("NFKC", name).split())
    cleaned_aliases = tuple(
        dict.fromkeys(
            cleaned
            for raw in aliases
            if (cleaned := " ".join(unicodedata.normalize("NFKC", raw).split()))
            and _normalize(cleaned) != _normalize(cleaned_name)
        )
    )
    cleaned_groups = tuple(dict.fromkeys(group.strip() for group in groups if group.strip()))
    cleaned_categories = tuple(dict.fromkeys(categories)) or (MediaCategory.OTHER,)
    match_names = tuple(value for value in (cleaned_name, *cleaned_aliases) if _is_safe_match_name(value))
    return NonJavActorProfile(
        name=cleaned_name,
        aliases=cleaned_aliases,
        groups=cleaned_groups,
        categories=cleaned_categories,
        match_names=match_names,
        image_file=image_file,
        biography=biography.strip() if biography and biography.strip() else None,
        notes=notes.strip() if notes and notes.strip() else None,
    )


def match_non_jav_actor_directory(
    directory_names: tuple[str, ...],
    catalog: NonJavActorCatalog,
) -> NonJavActorProfile | None:
    """Match only a complete directory component, avoiding broad substring guesses."""

    normalized_directories = {_normalize(name).lstrip("@"): name for name in directory_names}
    for actor in catalog.actors:
        for match_name in actor.match_names:
            if _normalize(match_name).lstrip("@") in normalized_directories:
                return actor
    return None


def _heading_metadata(line: str) -> tuple[str, MediaCategory] | None:
    normalized = line.casefold()
    if "x热门博主" in normalized:
        if "91探花" in normalized:
            return "x-91-tanhua", MediaCategory.CHINA
        if "杏吧" in normalized:
            return "x-xingba", MediaCategory.CHINA
        if any(marker in normalized for marker in ("探花", "探店", "约炮")):
            return "x-tanhua", MediaCategory.CHINA
        return "x", MediaCategory.OTHER
    rules = (
        (("91探花",), "91-tanhua", MediaCategory.CHINA),
        (("麻豆",), "madou", MediaCategory.CHINA),
        (("杏吧", "性吧"), "xingba", MediaCategory.CHINA),
        (("探店",), "tandian", MediaCategory.CHINA),
        (("约炮",), "yuepao", MediaCategory.CHINA),
        (("探花平台",), "tanhua", MediaCategory.CHINA),
        (("swag",), "swag", MediaCategory.CHINA),
        (("欧美",), "western", MediaCategory.EUROPE),
        (("onlyfans",), "onlyfans", MediaCategory.OTHER),
        (("twitter", "tiwwer"), "twitter", MediaCategory.OTHER),
        (("其他平台", "其他平台/渠道"), "independent", MediaCategory.OTHER),
    )
    if not (_MARKDOWN.match(line) or "热门" in line or "平台" in line):
        return None
    for markers, group, category in rules:
        if any(marker in normalized for marker in markers):
            return group, category
    return None


def _parse_actor_fields(fields: tuple[str, ...]) -> tuple[str, tuple[str, ...]]:
    raw_alias, raw_canonical, *extras = fields
    canonical_candidates = _name_variants(raw_canonical)
    if not canonical_candidates:
        return "", ()
    canonical = canonical_candidates[0]
    alias_variants: list[str] = []
    for value in (raw_alias, raw_canonical, *extras):
        alias_variants.extend(_name_variants(value))
    first_cleaned = _clean_annotation(raw_alias)
    if _normalize(first_cleaned) == _normalize(canonical):
        canonical = first_cleaned
    aliases = tuple(dict.fromkeys(value for value in alias_variants if value))
    return canonical, aliases


def _name_variants(value: str) -> tuple[str, ...]:
    normalized = " ".join(unicodedata.normalize("NFKC", value).split()).strip(" ,")
    if not normalized:
        return ()
    if match := _HANDLE_SUFFIX.fullmatch(normalized):
        return (match.group(1).strip(), match.group(2), f"@{match.group(2)}")
    cleaned = _clean_annotation(normalized)
    variants: list[str] = [cleaned]
    if cleaned != normalized:
        opening = normalized.rfind("(")
        if opening > 0:
            inner = normalized[opening + 1 :].rstrip(")").strip()
            if re.fullmatch(r"[A-Za-z0-9_ @.-]{2,32}", inner):
                variants.append(inner)
    return tuple(dict.fromkeys(value for value in variants if value))


def _clean_annotation(value: str) -> str:
    return _PARENTHETICAL.sub("", value).strip()


def _is_generic(value: str) -> bool:
    normalized = _normalize(value)
    return normalized in _GENERIC_NAMES or "系列女优" in normalized or "标题兜底" in normalized


def _is_safe_match_name(value: str) -> bool:
    normalized = _normalize(value).lstrip("@")
    if not normalized or _is_generic(normalized):
        return False
    if normalized.isascii():
        if normalized in _AMBIGUOUS_ASCII_NAMES:
            return False
        return len(re.sub(r"[^a-z0-9]", "", normalized)) >= 4
    return len(normalized) >= 2


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()
