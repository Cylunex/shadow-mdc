"""Normalize provider genre tags to stable display labels (CN or loanword).

Only translates / aliases / filters existing provider tags — never invents genres
for works that lack them. Internal seed and source markers are blacklisted from
display and facets.
"""

from __future__ import annotations

import json
import unicodedata
from collections import Counter
from collections.abc import Iterable, Iterator, Sequence
from contextlib import contextmanager
from contextvars import ContextVar
from functools import lru_cache

from .tag_models import TagDefinition, TagReference, TagRegistry, default_registry, fold_tag

_ACTIVE_REGISTRY: ContextVar[TagRegistry | None] = ContextVar("mdc_tag_dictionary", default=None)


def current_registry() -> TagRegistry:
    return _ACTIVE_REGISTRY.get() or default_registry()


@contextmanager
def use_registry(registry: TagRegistry) -> Iterator[None]:
    token = _ACTIVE_REGISTRY.set(registry)
    try:
        yield
    finally:
        _ACTIVE_REGISTRY.reset(token)


def _fold(value: str) -> str:
    return fold_tag(value)


def is_noise_tag(raw: str | None) -> bool:
    """Return True when a tag is an internal seed / source marker."""

    if raw is None:
        return True
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return True
    key = text.casefold()
    registry = current_registry()
    registered = registry.resolve(text)
    if registered is not None and registered.id in registry.ids:
        return registered.hidden
    if key in registry.noise_exact:
        return True
    return any(key.startswith(prefix) for prefix in registry.noise_prefixes)


def resolve_tag(raw: str | None) -> TagDefinition | None:
    """Resolve a stable identity without changing stored provider values."""
    if raw is None or is_noise_tag(raw):
        return None
    tag = current_registry().resolve(raw)
    return tag if tag is not None and not is_noise_tag(tag.name) else None


def tag_id(raw: str | None) -> str | None:
    """Stable ID for a source value, historical name, alias or existing ID."""
    tag = resolve_tag(raw)
    return tag.id if tag else None


def synonyms_for(raw: str) -> tuple[str, ...]:
    """Known alias forms for any ID/name; excludes the current display name."""
    tag = resolve_tag(raw)
    return tuple(item for item in tag.aliases if item != tag.name) if tag else ()


def canonicalize_tag(raw: str | None) -> str | None:
    """Compatibility display label; use tag_id for identity comparisons."""
    tag = resolve_tag(raw)
    return tag.name if tag else None


def tag_refs(raw: Iterable[str] | None) -> list[TagReference]:
    """Deterministic, deduplicated identities and names, with noise excluded."""
    by_id: dict[str, TagDefinition] = {}
    for item in raw or ():
        tag = resolve_tag(item)
        if tag is not None:
            previous = by_id.get(tag.id)
            if previous is None or tag.name < previous.name:
                by_id[tag.id] = tag
    ordered = sorted(
        by_id.values(), key=lambda tag: (current_registry().priority.get(tag.id, 10_000), _fold(tag.name))
    )
    # Per-work responses carry references; aliases belong in the shared facet dictionary.
    return [TagReference(tag.id, tag.name) for tag in ordered]


def normalize_tags(raw: Iterable[str] | None) -> list[str]:
    """Translate / alias / filter tags → deterministic, deduped display list."""

    return [tag.name for tag in tag_refs(raw)]


def visible_tree_refs(raw: Iterable[str] | None) -> list[TagReference]:
    """Visible node/ancestor references, deduplicated per work; source assignments stay direct."""
    registry = current_registry()
    found: dict[str, TagReference] = {}
    for value in raw or ():
        if not isinstance(value, str):
            continue
        direct = registry.resolve(value)
        if direct is None or (direct.id not in registry.ids and is_noise_tag(value)):
            continue
        for identity in registry.ancestor_ids(direct.id):
            node = registry.resolve(identity) if identity in registry.ids else direct
            if node is not None and not is_noise_tag(node.name):
                found[identity] = TagReference(identity, node.name)
    return list(found.values())


def tag_tree_ids(raw: str | None) -> str:
    """Semantic SQLite projection; visibility is applied after distinct subtree counts."""
    if not isinstance(raw, str):
        return "[]"
    registry = current_registry()
    tag = registry.resolve(raw)
    if tag is None or (tag.id not in registry.ids and is_noise_tag(raw)):
        return "[]"
    return json.dumps(registry.ancestor_ids(tag.id))


def tag_matches(raw: str | None, selected: str | None) -> bool:
    """A selected node matches itself and descendants, including historical source aliases."""
    if not isinstance(raw, str) or not isinstance(selected, str):
        return False
    registry = current_registry()
    direct, wanted = registry.resolve(raw), registry.resolve(selected)
    return (direct is not None and wanted is not None
            and registry.is_descendant(direct.id, wanted.id))


def work_matches_tags(work_tags: Sequence[str] | None, selected: Sequence[str]) -> bool:
    """AND filter: every selected tag must match the work's normalized set.

    Selected values may be JP/EN/CN synonyms — they are canonicalized first.
    """

    registry = current_registry()
    wanted: list[str | None] = []
    for item in selected:
        node = registry.resolve(item)
        if is_noise_tag(item) and (node is None or node.id not in registry.ids):
            continue
        wanted.append(node.id if node is not None else None)
    if any(item is None for item in wanted):
        return False
    wanted_clean = [item for item in wanted if item]
    if not wanted_clean:
        return True
    return all(any(tag_matches(raw, tag) for raw in work_tags or ()) for tag in wanted_clean)


def facet_tags(
    works_tags: Iterable[Sequence[str] | None],
    *,
    limit: int = 40,
    always_include: Sequence[str] | None = None,
) -> list[tuple[str, int]]:
    """Popular normalized tags with counts for the filter bar.

    Excludes noise. Returns up to ``limit`` by count, then ensures any
    ``always_include`` tag with count > 0 is present.
    """

    counter: Counter[str] = Counter()
    names: dict[str, str] = {}
    for tags in works_tags:
        for tag in visible_tree_refs(tags):
            counter[tag.id] += 1
            names.setdefault(tag.id, tag.name)
    ranked = sorted(counter.items(), key=lambda item: (-item[1], _fold(names[item[0]])))
    selected: list[tuple[str, int]] = ranked[: max(0, limit)]
    present = {name for name, _ in selected}
    for name in always_include if always_include is not None else current_registry().rules.common_filter_tags:
        identity = tag_id(name)
        if identity is None:
            continue
        count = counter.get(identity, 0)
        if count > 0 and identity not in present:
            selected.append((identity, count))
            present.add(identity)
    selected.sort(
        key=lambda item: (
            -item[1],
            current_registry().priority.get(item[0], 10_000),
            _fold(names[item[0]]),
        )
    )
    return [(names[identity], count) for identity, count in selected]


_META_SKIP = frozenset({"unknown", "other", ""})


def display_chips(
    *,
    category: str | None = None,
    family: str | None = None,
    studio: str | None = None,
    label: str | None = None,
    series: str | None = None,
    tags: Sequence[str] | None = None,
    max_genres: int = 12,
    max_total: int = 16,
) -> list[str]:
    """Build UI chips: normalized genres first, then light metadata.

    Memoized on the chip inputs — a works-grid render rebuilds chips for every
    card, and studio/series/tag combinations repeat heavily.
    """

    return list(
        _display_chips_cached(
            current_registry(),
            category,
            family,
            studio,
            label,
            series,
            tuple(tags) if tags else (),
            max_genres,
            max_total,
        )
    )


@lru_cache(maxsize=8_192)
def _display_chips_cached(
    registry: TagRegistry,
    category: str | None,
    family: str | None,
    studio: str | None,
    label: str | None,
    series: str | None,
    tags: tuple[str, ...],
    max_genres: int,
    max_total: int,
) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        text = unicodedata.normalize("NFKC", str(raw)).strip()
        if not text or text.casefold() in _META_SKIP:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        values.append(text)

    genres = normalize_tags(tags)[:max_genres]
    for genre in genres:
        add(genre)
    add(category)
    if family and family.casefold() not in {"unknown", "jav"}:
        add(family)
    add(studio)
    add(label)
    add(series)
    return tuple(values[:max_total])
