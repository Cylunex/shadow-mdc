import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from ..db.models import Actor, Work
from ..enums import ContentFamily, MediaCategory
from ..identity import IdentityAliasRules, extract_code

_DOMAIN_ACTOR = re.compile(r"(?i)^(?:www\.)?[a-z0-9-]+\.(?:com|net|org|tv|cc|me|xyz|top|vip)$")
_INVALID_ACTOR_NAMES = frozenset({"unknown", "uncategorized", "未知", "未归类", "未歸類", "未分类", "未分類"})


class ActorWorkReference(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str
    title: str
    code: str | None
    category: str
    image_url: str | None = None


class ActorProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str | None = None
    name: str
    aliases: tuple[str, ...]
    categories: tuple[str, ...]
    work_count: int
    works: tuple[ActorWorkReference, ...]
    image_url: str | None = None


class ActorCatalogPayload(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    profiles: tuple[ActorProfile, ...] = ()


class ActorCatalogStore:
    """Durable actor knowledge that is intentionally independent of the media database."""

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> tuple[ActorProfile, ...]:
        if not self._path.is_file():
            return ()
        try:
            payload = ActorCatalogPayload.model_validate_json(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError) as exc:
            raise ValueError(f"cannot read actor catalog: {exc}") from exc
        return payload.profiles

    def save(self, profiles: tuple[ActorProfile, ...]) -> tuple[ActorProfile, ...]:
        payload = ActorCatalogPayload(profiles=profiles)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        serialized = payload.model_dump_json(indent=2) + "\n"
        try:
            if self._path.is_file() and self._path.read_text(encoding="utf-8-sig") == serialized:
                return profiles
        except (OSError, UnicodeError):
            pass
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(serialized, encoding="utf-8")
        temporary.replace(self._path)
        return profiles


@dataclass
class _ActorAccumulator:
    name: str
    id: str | None = None
    image_url: str | None = None
    aliases: set[str] = field(default_factory=set)
    categories: set[str] = field(default_factory=set)
    works: list[ActorWorkReference] = field(default_factory=list)


def build_actor_catalog(
    works: list[Work],
    rules: IdentityAliasRules,
) -> tuple[ActorProfile, ...]:
    alias_to_canonical = {
        _normalize(alias): canonical.strip()
        for alias, canonical in rules.actors.items()
        if alias.strip() and canonical.strip()
    }
    accumulators: dict[str, _ActorAccumulator] = {}
    for work in works:
        for raw_name in work.actors:
            name = raw_name.strip()
            if not name:
                continue
            canonical = alias_to_canonical.get(_normalize(name), name)
            key = _normalize(canonical)
            accumulator = accumulators.setdefault(key, _ActorAccumulator(name=canonical))
            accumulator.categories.add(work.category)
            if all(reference.id != work.id for reference in accumulator.works):
                accumulator.works.append(
                    ActorWorkReference(
                        id=work.id,
                        title=work.title,
                        code=work.primary_code,
                        category=work.category,
                        image_url=_work_image_url(work),
                    )
                )
    for alias, canonical in rules.actors.items():
        matched = accumulators.get(_normalize(canonical))
        if matched is not None and _normalize(alias) != _normalize(canonical):
            matched.aliases.add(alias.strip())
    return _profiles_from_accumulators(accumulators)


def build_actor_catalog_from_relations(
    relations: list[tuple[Actor, Work]],
    rules: IdentityAliasRules,
) -> tuple[ActorProfile, ...]:
    alias_to_canonical = {
        _normalize(alias): canonical.strip()
        for alias, canonical in rules.actors.items()
        if alias.strip() and canonical.strip()
    }
    accumulators: dict[str, _ActorAccumulator] = {}
    for actor, work in relations:
        canonical = alias_to_canonical.get(_normalize(actor.name), actor.name)
        key = _normalize(canonical)
        accumulator = accumulators.setdefault(
            key,
            _ActorAccumulator(
                name=canonical,
                id=actor.id,
                image_url=actor.image_url,
            ),
        )
        accumulator.aliases.update(alias for alias in actor.aliases if alias.strip())
        if _normalize(actor.name) != key:
            accumulator.aliases.add(actor.name)
        accumulator.categories.add(work.category)
        if all(reference.id != work.id for reference in accumulator.works):
            accumulator.works.append(
                ActorWorkReference(
                    id=work.id,
                    title=work.title,
                    code=work.primary_code,
                    category=work.category,
                    image_url=_work_image_url(work),
                )
            )
    for alias, canonical in rules.actors.items():
        matched = accumulators.get(_normalize(canonical))
        if matched is not None and _normalize(alias) != _normalize(canonical):
            matched.aliases.add(alias.strip())
    return _profiles_from_accumulators(accumulators)


def enrich_actor_aliases(
    rules: IdentityAliasRules,
    catalog: tuple[ActorProfile, ...],
) -> IdentityAliasRules:
    actors = dict(rules.actors)
    for profile in catalog:
        actors.setdefault(profile.name, profile.name)
        for alias in profile.aliases:
            actors.setdefault(alias, profile.name)
    return rules.model_copy(update={"actors": actors})


def merge_actor_catalogs(
    persisted: tuple[ActorProfile, ...],
    current: tuple[ActorProfile, ...],
    rules: IdentityAliasRules,
) -> tuple[ActorProfile, ...]:
    """Merge durable and current profiles without losing works removed from the media database."""

    alias_to_canonical = {
        _normalize(alias): canonical.strip()
        for alias, canonical in rules.actors.items()
        if alias.strip() and canonical.strip()
    }
    accumulators: dict[str, _ActorAccumulator] = {}
    work_keys: dict[str, dict[tuple[str, str], int]] = {}
    for profile in (*persisted, *current):
        canonical = alias_to_canonical.get(_normalize(profile.name), profile.name.strip())
        key = _normalize(canonical)
        accumulator = accumulators.setdefault(key, _ActorAccumulator(name=canonical))
        if profile.id is not None:
            accumulator.id = profile.id
        if profile.image_url is not None:
            accumulator.image_url = profile.image_url
        if _normalize(profile.name) != key:
            accumulator.aliases.add(profile.name.strip())
        accumulator.aliases.update(alias.strip() for alias in profile.aliases if alias.strip())
        accumulator.categories.update(profile.categories)
        indexes = work_keys.setdefault(key, {})
        for work in profile.works:
            identity = _work_identity(work)
            existing_index = indexes.get(identity)
            if existing_index is None:
                indexes[identity] = len(accumulator.works)
                accumulator.works.append(work)
            else:
                accumulator.works[existing_index] = work

    profiles = [
        ActorProfile(
            id=item.id,
            name=item.name,
            aliases=tuple(
                sorted(
                    (alias for alias in item.aliases if _normalize(alias) != _normalize(item.name)),
                    key=str.casefold,
                )
            ),
            categories=tuple(sorted(item.categories)),
            work_count=len(item.works),
            works=tuple(sorted(item.works, key=lambda work: (work.category, work.code or "", work.title))),
            image_url=item.image_url
            or next(
                (work.image_url for work in item.works if work.image_url),
                None,
            ),
        )
        for item in accumulators.values()
    ]
    return tuple(sorted(profiles, key=lambda item: (-item.work_count, item.name.casefold())))


def sync_actor_catalog(
    store: ActorCatalogStore,
    works: list[Work],
    rules: IdentityAliasRules,
) -> tuple[ActorProfile, ...]:
    merged = merge_actor_catalogs(store.load(), build_actor_catalog(works, rules), rules)
    return store.save(filter_jav_actor_catalog(merged))


def sync_actor_catalog_from_relations(
    store: ActorCatalogStore,
    relations: list[tuple[Actor, Work]],
    rules: IdentityAliasRules,
) -> tuple[ActorProfile, ...]:
    current = build_actor_catalog_from_relations(relations, rules)
    merged = merge_actor_catalogs(store.load(), current, rules)
    return store.save(filter_jav_actor_catalog(merged))


def filter_jav_actor_catalog(
    profiles: tuple[ActorProfile, ...],
) -> tuple[ActorProfile, ...]:
    """Keep only actor-work knowledge backed by an actual JAV code."""

    filtered: list[ActorProfile] = []
    for profile in profiles:
        if _is_invalid_actor_name(profile.name):
            continue
        works = tuple(work for work in profile.works if _is_jav_work(work))
        if not works:
            continue
        filtered.append(
            profile.model_copy(
                update={
                    "categories": (MediaCategory.JAPAN.value,),
                    "work_count": len(works),
                    "works": works,
                    "image_url": profile.image_url
                    or next(
                        (work.image_url for work in works if work.image_url),
                        None,
                    ),
                }
            )
        )
    return tuple(sorted(filtered, key=lambda item: (-item.work_count, item.name.casefold())))


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _profiles_from_accumulators(
    accumulators: dict[str, _ActorAccumulator],
) -> tuple[ActorProfile, ...]:
    profiles = [
        ActorProfile(
            id=item.id,
            name=item.name,
            aliases=tuple(sorted(item.aliases, key=str.casefold)),
            categories=tuple(sorted(item.categories)),
            work_count=len(item.works),
            works=tuple(sorted(item.works, key=lambda work: (work.category, work.code or "", work.title))),
            image_url=item.image_url
            or next(
                (work.image_url for work in item.works if work.image_url),
                None,
            ),
        )
        for item in accumulators.values()
    ]
    return tuple(sorted(profiles, key=lambda item: (-item.work_count, item.name.casefold())))


def _work_image_url(work: Work) -> str | None:
    poster_items = [
        item
        for item in work.artwork
        if str(item.get("kind", "thumb")).casefold() not in {"fanart", "background", "backdrop"}
    ]
    for item in poster_items:
        local_path = item.get("local_path")
        if isinstance(local_path, str) and Path(local_path).is_file():
            return f"/api/works/{work.id}/artwork/poster"
    for item in poster_items:
        url = item.get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    for item in work.artwork:
        local_path = item.get("local_path")
        if isinstance(local_path, str) and Path(local_path).is_file():
            return f"/api/works/{work.id}/artwork/fanart"
    return next(
        (
            url
            for item in work.artwork
            if isinstance((url := item.get("url")), str) and url.startswith(("http://", "https://"))
        ),
        None,
    )


def _work_identity(work: ActorWorkReference) -> tuple[str, str]:
    identity = work.code if work.code else work.title
    return work.category.casefold(), _normalize(identity)


def _is_jav_work(work: ActorWorkReference) -> bool:
    if work.category != MediaCategory.JAPAN.value or work.code is None:
        return False
    code, family = extract_code(work.code, MediaCategory.OTHER)
    return code is not None and family is ContentFamily.JAV


def _is_invalid_actor_name(name: str) -> bool:
    normalized = _normalize(name)
    return normalized in _INVALID_ACTOR_NAMES or _DOMAIN_ACTOR.fullmatch(normalized) is not None
