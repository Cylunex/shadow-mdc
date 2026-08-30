import os
from collections.abc import Iterator
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..db.models import Library
from ..db.repository import Repository
from ..domain import MediaTechnicalInfo
from ..enums import ContentFamily, MediaCategory, QueryMode, RecognitionScope
from ..identity import IdentityAliasRules, build_identity_hints
from ..media.oshash import compute_oshash
from ..media.probe import probe_media_info
from ..media.strm import read_strm_locator, redact_media_locator
from .directory_actor_rules import DirectoryActorRules
from .local_catalog import (
    build_local_catalog_record,
    is_generic_file_name,
    is_video_part_name,
    local_context_names,
)
from .non_jav_actor_catalog import NonJavActorCatalog, match_non_jav_actor_directory
from .path_filter import MediaPathFilter

VIDEO_EXTENSIONS = frozenset(
    {
        ".3gp",
        ".avi",
        ".f4v",
        ".flv",
        ".iso",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".rm",
        ".rmvb",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
    }
)
MEDIA_EXTENSIONS = VIDEO_EXTENSIONS | {".strm"}
IGNORED_DIRECTORY_NAMES = frozenset(
    {
        "#recycle",
        "#不要扫描",
        "#整理完成",
        "$recycle.bin",
        ".actors",
        ".amane_trash",
        "@eadir",
        "extrafanart",
        "lost+found",
        "system volume information",
    }
)


class ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    discovered: int
    updated: int
    queued: int
    identified: int
    filtered: int
    skipped: int
    errors: tuple[str, ...]


class Scanner:
    def __init__(
        self,
        repository: Repository,
        alias_rules: IdentityAliasRules | None = None,
        path_filter: MediaPathFilter | None = None,
        directory_actor_rules: DirectoryActorRules | None = None,
        non_jav_actor_catalog: NonJavActorCatalog | None = None,
    ):
        self._repository = repository
        self._alias_rules = alias_rules or IdentityAliasRules()
        self._path_filter = path_filter or MediaPathFilter()
        self._directory_actor_rules = directory_actor_rules or DirectoryActorRules()
        self._non_jav_actor_catalog = non_jav_actor_catalog or NonJavActorCatalog()

    def scan(self, library: Library) -> ScanResult:
        root = _resolve_library_root(library.root_path)
        discovered = 0
        updated = 0
        queued = 0
        identified = 0
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
                created, newly_queued = self._scan_asset(library, root, path)
                discovered += int(created)
                updated += int(not created)
                queued += int(newly_queued)
                identified += int(not newly_queued)
            except (OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
        return ScanResult(
            discovered=discovered,
            updated=updated,
            queued=queued,
            identified=identified,
            filtered=filtered,
            skipped=skipped,
            errors=tuple(errors),
        )

    def _scan_asset(self, library: Library, root: Path, path: Path) -> tuple[bool, bool]:
        stat = path.stat()
        is_strm = path.suffix.casefold() == ".strm"
        existing = self._repository.get_asset_by_path(str(path))
        unchanged = bool(
            existing and existing.size == stat.st_size and existing.modified_ns == stat.st_mtime_ns
        )
        raw_media_locator = read_strm_locator(path) if is_strm else None
        existing_media_info = (
            MediaTechnicalInfo.model_validate(existing.media_info)
            if existing is not None and existing.media_info
            else None
        )
        media_info = (
            existing_media_info
            if unchanged and existing_media_info is not None
            else (MediaTechnicalInfo() if is_strm else probe_media_info(path))
        )
        duration = media_info.duration_seconds
        oshash = (
            existing.oshash
            if unchanged and existing is not None
            else (None if is_strm else compute_oshash(path))
        )
        fingerprints = {"oshash": oshash} if oshash else {}
        context_names = local_context_names(path, root)
        hints = build_identity_hints(
            path,
            fingerprints=fingerprints,
            duration_seconds=duration,
            media_locator=raw_media_locator,
            context_names=context_names,
            alias_rules=self._alias_rules,
            category=MediaCategory.OTHER,
        )
        if raw_media_locator is not None:
            hints = hints.model_copy(update={"media_locator": redact_media_locator(raw_media_locator)})
        directory_rule = self._directory_actor_rules.match(path, root)
        directory_profile = match_non_jav_actor_directory(
            context_names,
            self._non_jav_actor_catalog,
        )
        confirmed_directory_actor = directory_rule is not None or (
            directory_profile is not None and is_generic_file_name(path.stem)
        )
        if directory_rule is not None:
            hints = hints.model_copy(
                update={
                    "actors": (directory_rule.actor,),
                    "category": directory_rule.category,
                    "family": _family_for_category(directory_rule.category),
                    "alias_evidence": (*hints.alias_evidence, "directory-actor:confirmed"),
                }
            )
        elif directory_profile is not None and is_generic_file_name(path.stem):
            category = directory_profile.categories[0] if directory_profile.categories else hints.category
            hints = hints.model_copy(
                update={
                    "actors": (directory_profile.name,),
                    "category": category,
                    "family": _family_for_category(category),
                    "alias_evidence": (*hints.alias_evidence, "directory-actor:catalog"),
                }
            )
        local_record = build_local_catalog_record(
            library_id=library.id,
            root=root,
            path=path,
            hints=hints,
            actor_directory=Path(directory_rule.directory) if directory_rule is not None else None,
        )
        if hints.code is None and is_video_part_name(path.stem):
            hints = hints.model_copy(
                update={
                    "term": local_record.title,
                    "mode": QueryMode.TEXT,
                    "title": local_record.title,
                    "family": local_record.family,
                    "category": local_record.category,
                    "studio": local_record.studio or hints.studio,
                    "series": local_record.series or hints.series,
                    "actors": local_record.actors or hints.actors,
                }
            )
        asset, created = self._repository.upsert_asset(
            library_id=library.id,
            path=str(path),
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            duration_seconds=duration,
            oshash=oshash,
            hints=hints,
            media_info=media_info,
        )
        if asset.work_id is None and hints.code:
            existing_work = self._repository.find_work_by_code(hints.code)
            if existing_work is not None:
                self._repository.attach_asset_to_work(asset, existing_work)
        if (
            hints.code is None
            and confirmed_directory_actor
            and library.recognition_scope != RecognitionScope.JAV_ONLY.value
        ):
            self._repository.catalog_asset(asset, local_record)
            return created, False
        newly_queued = self._repository.queue_local_candidate(
            asset,
            local_record,
        )
        return created, newly_queued


def _family_for_category(category: MediaCategory) -> ContentFamily:
    return {
        MediaCategory.JAPAN: ContentFamily.UNKNOWN,
        MediaCategory.CHINA: ContentFamily.CHINESE,
        MediaCategory.KOREA: ContentFamily.KOREAN,
        MediaCategory.EUROPE: ContentFamily.WESTERN,
        MediaCategory.OTHER: ContentFamily.UNKNOWN,
    }[category]


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
                elif (
                    recursive
                    and entry.is_dir(follow_symlinks=False)
                    and entry.name.casefold() not in IGNORED_DIRECTORY_NAMES
                ):
                    pending.append(Path(entry.path))
            except OSError as exc:
                errors.append(f"{entry.path}: cannot inspect entry: {exc}")
