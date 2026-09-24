"""Offline r18.dev dump provider (local SQLite; no network)."""

from __future__ import annotations

from pathlib import Path

from ..domain import IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from ..services.r18_dump import PROVIDER_ID, R18DumpStore


class R18DumpProvider:
    """Looks up JAV metadata from an imported r18.dev dump SQLite file."""

    def __init__(self, db_path: Path | None = None, store: R18DumpStore | None = None):
        if store is None and db_path is None:
            raise ValueError("r18 dump provider requires db_path or store")
        self._db_path = Path(db_path) if db_path is not None else None
        self._store = store
        self._owns_store = store is None

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=PROVIDER_ID,
            name="R18.dev dump (offline)",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    def _get_store(self) -> R18DumpStore | None:
        if self._store is not None:
            return self._store
        if self._db_path is None or not self._db_path.is_file():
            return None
        self._store = R18DumpStore(self._db_path)
        return self._store

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        code, family = extract_code(requested)
        if code is None or family is not ContentFamily.JAV or code.startswith("FC2-"):
            return []
        store = self._get_store()
        if store is None:
            return []
        record = store.build_record(code)
        return [record] if record is not None else []

    def close(self) -> None:
        if self._owns_store and self._store is not None:
            self._store.close()
            self._store = None

