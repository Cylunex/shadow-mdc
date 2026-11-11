"""Persistent tag vocabulary and display rules, separate from application code."""

from __future__ import annotations

import fcntl
import os
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from threading import RLock
from urllib.parse import quote

from ..tag_models import TagDocument, TagRegistry, TagRules, TagTerm, fold_tag, seed_document


@dataclass(frozen=True)
class ImportedTag:
    source_id: str
    name: str
    aliases: tuple[str, ...]
    group: str


class TagRevisionConflict(ValueError):
    """The caller edited an older vocabulary revision."""


class TagDictionaryStore:
    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = RLock()
        self._signature: tuple[int, int, int] | None = None
        self._document: TagDocument | None = None
        self._registry: TagRegistry | None = None

    @contextmanager
    def _writer(self) -> Iterator[None]:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._lock, self.path.with_suffix(".lock").open("a") as lock:
            os.fchmod(lock.fileno(), 0o600)
            fcntl.flock(lock, fcntl.LOCK_EX)
            yield

    def initialize(self) -> None:
        """Import an optional external seed once; preserve existing runtime edits."""
        with self._writer():
            if not self.path.exists():
                self._write(seed_document())
        self.load()

    def load(self) -> TagDocument:
        with self._lock:
            stat = self.path.stat()
            signature = (stat.st_ino, stat.st_mtime_ns, stat.st_size)
            if self._document is None or self._signature != signature:
                document = TagDocument.model_validate_json(self.path.read_text(encoding="utf-8"))
                registry = document.registry()
                self._document, self._registry, self._signature = document, registry, signature
            return self._document

    def registry(self) -> TagRegistry:
        self.load()
        assert self._registry is not None
        return self._registry

    def _write(self, document: TagDocument) -> None:
        document.registry()  # Reject ambiguous aliases before writing any data.
        temp = self.path.with_suffix(".tmp")
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            os.fchmod(stream.fileno(), 0o600)
            stream.write(document.model_dump_json(indent=2) + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp, self.path)
        self._signature = None

    def merge_imports(self, values: list[ImportedTag]) -> dict[str, str]:
        """Merge external terms before DB migration; a retry never changes existing identities."""
        with self._writer():
            document = self.load()
            terms = {term.id: term for term in document.terms}
            identities: dict[str, str] = {}
            for value in values:
                registry = document.model_copy(update={"terms": list(terms.values())}).registry()
                matches = {
                    tag.id
                    for name in (value.name, *value.aliases)
                    if (tag := registry.resolve(name)) is not None and tag.id in registry.ids
                }
                primary = registry.resolve(value.name)
                if primary is not None and primary.id in registry.ids:
                    identity = primary.id
                elif len(matches) == 1:
                    identity = next(iter(matches))
                else:
                    # Ambiguous aliases cannot select between existing identities.
                    # Preserve the explicit genre name as its own stable tag instead.
                    identity = "raw:" + quote(fold_tag(value.name), safe="")
                safe_names = tuple(
                    name
                    for name in (value.name, *value.aliases)
                    if (resolved := registry.resolve(name)) is None
                    or resolved.id not in registry.ids
                    or resolved.id == identity
                )
                old = terms.get(identity)
                if old is None:
                    term = TagTerm(id=identity, name=value.name, aliases=safe_names[:200], group=value.group)
                else:
                    aliases = tuple(dict.fromkeys((*old.aliases, *safe_names)))[:200]
                    term = old.model_copy(update={"aliases": aliases, "group": old.group or value.group})
                    if term != old:
                        term = term.model_copy(update={"revision": old.revision + 1})
                terms[identity] = term
                identities[value.source_id] = identity
            if list(terms.values()) != document.terms:
                self._write(
                    document.model_copy(
                        update={
                            "terms": list(terms.values()),
                            "revision": document.revision + 1,
                        }
                    )
                )
            return identities

    def create(self, name: str, aliases: tuple[str, ...], hidden: bool, group: str = "") -> TagTerm:
        with self._writer():
            document = self.load()
            term = TagTerm(
                id="raw:" + quote(fold_tag(name), safe=""),
                name=name.strip(),
                aliases=aliases,
                hidden=hidden,
                group=group.strip(),
            )
            self._write(
                document.model_copy(
                    update={"terms": [*document.terms, term], "revision": document.revision + 1}
                )
            )
            return term

    def update(
        self,
        identity: str,
        name: str,
        aliases: tuple[str, ...] | None,
        hidden: bool | None,
        revision: int,
        group: str | None = None,
    ) -> TagTerm:
        with self._writer():
            document = self.load()
            old = next((term for term in document.terms if term.id == identity), None)
            if old is None:
                raw = document.registry().resolve(identity)
                if raw is None or raw.id != identity or not identity.startswith("raw:"):
                    raise KeyError(identity)
                # Promote a discovered source label without changing its existing raw ID.
                old = TagTerm(
                    id=identity,
                    name=raw.name,
                    hidden=raw.name.casefold() in document.rules.noise_exact
                    or any(
                        raw.name.casefold().startswith(prefix) for prefix in document.rules.noise_prefixes
                    ),
                )
            if old.revision != revision:
                raise TagRevisionConflict("tag revision conflict")
            term = TagTerm(
                id=identity,
                name=name.strip(),
                revision=revision + 1,
                hidden=old.hidden if hidden is None else hidden,
                aliases=tuple(dict.fromkeys([*(old.aliases if aliases is None else aliases), old.name])),
                group=old.group if group is None else group.strip(),
            )
            self._write(
                document.model_copy(
                    update={
                        "terms": [term if value.id == identity else value for value in document.terms]
                        + ([] if any(value.id == identity for value in document.terms) else [term]),
                        "revision": document.revision + 1,
                    }
                )
            )
            return term

    def update_rules(self, rules: TagRules, revision: int) -> TagDocument:
        with self._writer():
            document = self.load()
            if document.revision != revision:
                raise TagRevisionConflict("tag dictionary revision conflict")
            updated = document.model_copy(update={"rules": rules, "revision": revision + 1})
            self._write(updated)
            return updated
