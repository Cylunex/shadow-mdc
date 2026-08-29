import os
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..db.models import Library
from ..db.repository import Repository
from ..enums import AssetState, MediaCategory
from ..identity import IdentityAliasRules, build_identity_hints
from ..media.oshash import compute_oshash
from ..media.probe import probe_duration
from ..media.strm import read_strm_locator, redact_media_locator
from .local_catalog import build_local_catalog_record
from .path_filter import MediaPathFilter

VIDEO_EXTENSIONS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm"})
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {".strm"}


class ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    discovered: int
    updated: int
    cataloged: int
    filtered: int
    skipped: int
    errors: tuple[str, ...]


class Scanner:
    def __init__(
        self,
        repository: Repository,
        alias_rules: IdentityAliasRules | None = None,
        path_filter: MediaPathFilter | None = None,
    ):
        self._repository = repository
        self._alias_rules = alias_rules or IdentityAliasRules()
        self._path_filter = path_filter or MediaPathFilter()

    def scan(self, library: Library) -> ScanResult:
        root = _resolve_library_root(library.root_path)
        discovered = 0
        updated = 0
        cataloged = 0
        filtered = 0
        skipped = 0
        errors: list[str] = []
        for path in _walk_files(root, recursive=library.recursive, errors=errors):
            if path.suffix.casefold() not in MEDIA_EXTENSIONS:
                skipped += 1
                continue
            filter_match = self._path_filter.match(path, root)
            if filter_match is not None:
                self._repository.ignore_asset_by_path(
                    str(path),
                    f"filtered by path rule: {filter_match.word}",
                )
                filtered += 1
                continue
            try:
                created, newly_cataloged = self._scan_asset(library, root, path)
                discovered += int(created)
                updated += int(not created)
                cataloged += int(newly_cataloged)
            except (OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
        return ScanResult(
            discovered=discovered,
            updated=updated,
            cataloged=cataloged,
            filtered=filtered,
            skipped=skipped,
            errors=tuple(errors),
        )

    def _scan_asset(self, library: Library, root: Path, path: Path) -> tuple[bool, bool]:
        stat = path.stat()
        is_strm = path.suffix.casefold() == ".strm"
        raw_media_locator = read_strm_locator(path) if is_strm else None
        duration = None if is_strm else probe_duration(path)
        oshash = None if is_strm else compute_oshash(path)
        fingerprints = {"oshash": oshash} if oshash else {}
        hints = build_identity_hints(
            path,
            fingerprints=fingerprints,
            duration_seconds=duration,
            media_locator=raw_media_locator,
            context_names=_context_names(path, root),
            alias_rules=self._alias_rules,
            category=MediaCategory(library.category),
        )
        if raw_media_locator is not None:
            hints = hints.model_copy(
                update={"media_locator": redact_media_locator(raw_media_locator)}
            )
        asset, created = self._repository.upsert_asset(
            library_id=library.id,
            path=str(path),
            size=stat.st_size,
            duration_seconds=duration,
            oshash=oshash,
            hints=hints,
        )
        newly_cataloged = asset.state != AssetState.IDENTIFIED.value
        self._repository.catalog_asset(
            asset,
            build_local_catalog_record(
                library_id=library.id,
                root=root,
                path=path,
                hints=hints,
            ),
        )
        return created, newly_cataloged


def _resolve_library_root(value: str) -> Path:
    try:
        root = Path(value).resolve()
        if not root.is_dir():
            raise ValueError(f"library root is not a directory or is currently unavailable: {root}")
        return root
    except OSError as exc:
        raise ValueError(f"library root is unavailable: {value}: {exc}") from exc


def _walk_files(root: Path, *, recursive: bool, errors: list[str]) -> Iterator[Path]:
    pending = [root]
    while pending:
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            errors.append(f"{directory}: cannot list directory: {exc}")
            continue
        for entry in entries:
            try:
                if entry.is_file(follow_symlinks=False):
                    yield Path(entry.path)
                elif recursive and entry.is_dir(follow_symlinks=False):
                    pending.append(Path(entry.path))
            except OSError as exc:
                errors.append(f"{entry.path}: cannot inspect entry: {exc}")


def _context_names(path: Path, root: Path) -> tuple[str, ...]:
    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError:
        return ()
    return tuple(reversed(relative_parent.parts[-3:]))
