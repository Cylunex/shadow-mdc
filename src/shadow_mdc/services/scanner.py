import os
import shutil
from collections.abc import Callable, Iterator
from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict

from ..db.models import Library, MediaAsset, Work
from ..db.repository import Repository
from ..domain import MediaTechnicalInfo
from ..enums import ContentFamily, MediaCategory, QueryMode, RecognitionScope
from ..identity import IdentityAliasRules, build_identity_hints
from ..media.artwork import ensure_list_thumbnail
from ..media.nfo_import import (
    NfoImporter,
    NfoImportPolicy,
    apply_nfo_locked_fields,
    parse_movie_nfo,
    resolve_sidecar_nfo,
    sibling_artwork_paths,
)
from ..media.oshash import compute_oshash
from ..media.parts import (
    directory_volume_merge_code,
    volume_merge_identity_blocks,
)
from ..media.probe import probe_media_info
from ..media.strm import read_strm_locator, redact_media_locator
from ..normalize_code import is_format_equivalent
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
        artwork_dir: Path | None = None,
    ) -> None:
        self._repository = repository
        self._alias_rules = alias_rules or IdentityAliasRules()
        self._path_filter = path_filter or MediaPathFilter()
        self._directory_actor_rules = directory_actor_rules or DirectoryActorRules()
        self._non_jav_actor_catalog = non_jav_actor_catalog or NonJavActorCatalog()
        self._artwork_dir = artwork_dir
        self.last_obsolete_codes: tuple[str, ...] = ()

    def scan(
        self,
        library: Library,
        *,
        only_new: bool = False,
        progress: Callable[[dict[str, object]], None] | None = None,
        cancelled: Callable[[], bool] | None = None,
    ) -> ScanResult:
        root = _resolve_library_root(library.root_path)
        discovered = 0
        updated = 0
        queued = 0
        identified = 0
        filtered = 0
        skipped = 0
        errors: list[str] = []
        obsolete_codes: list[str] = []
        if progress is not None:
            progress({"progress_current": 0, "progress_total": 0, "phase": "discover"})
        paths = list(_walk_files(root, recursive=library.recursive, errors=errors, cancelled=cancelled))
        volume_codes = _directory_volume_codes(paths, cancelled=cancelled)
        total = len(paths)
        for index, path in enumerate(paths, start=1):
            if cancelled is not None and cancelled():
                break
            if progress is not None and (index == 1 or index % 25 == 0 or index == total):
                progress({"progress_current": index, "progress_total": total, "phase": "scan"})
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
            if only_new:
                existing = self._repository.get_asset_by_path(str(path))
                if existing is not None:
                    try:
                        stat = path.stat()
                    except OSError as exc:
                        errors.append(f"{path}: {exc}")
                        continue
                    if existing.size == stat.st_size and existing.modified_ns == stat.st_mtime_ns:
                        skipped += 1
                        continue
            try:
                created, newly_queued, obsolete = self._scan_asset(
                    library,
                    root,
                    path,
                    volume_code=volume_codes.get(path.parent),
                )
                discovered += int(created)
                updated += int(not created)
                queued += int(newly_queued)
                identified += int(not newly_queued)
                obsolete_codes.extend(obsolete)
            except (OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
        self.last_obsolete_codes = tuple(dict.fromkeys(obsolete_codes))
        return ScanResult(
            discovered=discovered,
            updated=updated,
            queued=queued,
            identified=identified,
            filtered=filtered,
            skipped=skipped,
            errors=tuple(errors),
        )

    def _scan_asset(
        self,
        library: Library,
        root: Path,
        path: Path,
        *,
        volume_code: str | None = None,
    ) -> tuple[bool, bool, tuple[str, ...]]:
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
        obsolete: list[str] = []
        merged_code = _apply_volume_merge_code(
            self._repository,
            path=path,
            existing=existing,
            hints_code=hints.code,
            volume_code=volume_code,
            obsolete=obsolete,
        )
        if merged_code:
            hints = hints.model_copy(
                update={
                    "code": merged_code,
                    "mode": QueryMode.CODE,
                    "term": merged_code,
                    "alias_evidence": (*hints.alias_evidence, "directory-volume-merge"),
                }
            )
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
        # NFO-first rebuild: trust sidecar metadata before remote scrape (miyabi-inspired).
        if asset.work_id is None and resolve_sidecar_nfo(path) is not None:
            result = NfoImporter(
                self._repository,
                policy=NfoImportPolicy(mode="trust"),
            ).import_library_assets([asset])
            if result.trusted:
                asset = self._repository.get_asset(asset.id) or asset
        elif asset.work_id is not None and (nfo_path := resolve_sidecar_nfo(path)) is not None:
            _adopt_nfo_manual_fields(self._repository, asset.work_id, nfo_path)
        if asset.work_id is not None and self._artwork_dir is not None:
            work = self._repository.get_work(asset.work_id)
            if work is not None:
                _adopt_sibling_artwork(self._repository, work, path, self._artwork_dir)
        if (
            hints.code is None
            and confirmed_directory_actor
            and library.recognition_scope != RecognitionScope.JAV_ONLY.value
        ):
            self._repository.catalog_asset(asset, local_record)
            return created, False, tuple(obsolete)
        newly_queued = self._repository.queue_local_candidate(
            asset,
            local_record,
        )
        return created, newly_queued, tuple(obsolete)


def _directory_volume_codes(
    paths: list[Path], *, cancelled: Callable[[], bool] | None = None
) -> dict[Path, str]:
    """Map parent directories to a volume-merge code when the lettered set qualifies."""

    by_parent: dict[Path, list[Path]] = {}
    for index, path in enumerate(paths):
        if index % 128 == 0 and cancelled is not None and cancelled():
            return {}
        if path.suffix.casefold() not in MEDIA_EXTENSIONS:
            continue
        # Walked paths are already rooted at the resolved library directory.
        parent = path.parent
        by_parent.setdefault(parent, []).append(path)

    result: dict[Path, str] = {}
    for parent, media in by_parent.items():
        if cancelled is not None and cancelled():
            break
        names: list[str] = []
        sizes: list[int | None] = []
        for item in media:
            names.append(item.name)
            try:
                sizes.append(item.stat().st_size)
            except OSError:
                sizes.append(None)
        sidecars: list[str] = []
        try:
            for entry in parent.iterdir():
                if entry.is_file() and entry.suffix.casefold() in {".nfo", ".txt"}:
                    sidecars.append(entry.name)
        except OSError:
            sidecars = []
        code = directory_volume_merge_code(parent, names, sidecars, sizes=sizes)
        if code:
            result[parent] = code
    return result


def _apply_volume_merge_code(
    repository: Repository,
    *,
    path: Path,
    existing: MediaAsset | None,
    hints_code: str | None,
    volume_code: str | None,
    obsolete: list[str],
) -> str | None:
    """Apply directory volume merge unless a confirmed identity conflicts."""

    if not volume_code:
        return None
    previous_codes: list[str] = []
    if hints_code and not is_format_equivalent(hints_code, volume_code):
        previous_codes.append(hints_code)
    if existing is not None and existing.work_id is not None:
        work = repository.get_work(existing.work_id)
        if work is not None and work.primary_code:
            confirmed = repository.work_has_verified_evidence(work.id)
            if volume_merge_identity_blocks(volume_code, work.primary_code, confirmed=confirmed):
                return None
            if not is_format_equivalent(work.primary_code, volume_code):
                previous_codes.append(work.primary_code)
    for code in previous_codes:
        if repository.find_work_by_code(code) is None:
            obsolete.append(code)
    return volume_code


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


def _walk_files(
    root: Path, *, recursive: bool, errors: list[str], cancelled: Callable[[], bool] | None = None
) -> Iterator[Path]:
    pending = [root]
    while pending:
        if cancelled is not None and cancelled():
            return
        directory = pending.pop()
        try:
            with os.scandir(directory) as iterator:
                entries = sorted(iterator, key=lambda item: item.name.casefold())
        except OSError as exc:
            errors.append(f"{directory}: cannot list directory: {exc}")
            continue
        for index, entry in enumerate(entries):
            if index % 128 == 0 and cancelled is not None and cancelled():
                return
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


def _adopt_nfo_manual_fields(repository: Repository, work_id: str, nfo_path: Path) -> None:
    """Re-scan: NFO fields the user marked manual become locked Work values (best-effort)."""

    work = repository.get_work(work_id)
    if work is None:
        return
    try:
        parsed = parse_movie_nfo(nfo_path)
    except (OSError, ElementTree.ParseError, ValueError):
        return
    if not parsed.locked_fields:
        return
    try:
        apply_nfo_locked_fields(repository, work, parsed)
    except ValueError:
        return


def _adopt_sibling_artwork(repository: Repository, work: Work, media_path: Path, artwork_dir: Path) -> None:
    """Copy NFO-adjacent poster/fanart into the local artwork cache and make a list thumb."""

    siblings = sibling_artwork_paths(media_path)
    if not siblings:
        return
    work_root = artwork_dir / work.id
    work_root.mkdir(parents=True, exist_ok=True)
    artwork = [dict(item) for item in (work.artwork or [])]
    changed = False
    for kind, source in siblings.items():
        suffix = source.suffix.casefold() if source.suffix else ".jpg"
        if suffix not in {".jpg", ".jpeg", ".png", ".webp", ".gif"}:
            suffix = ".jpg"
        destination = work_root / f"{kind}{suffix}"
        if not destination.is_file():
            shutil.copy2(source, destination)
            changed = True
        ensure_list_thumbnail(work_root, destination)
        already = any(
            isinstance(item.get("local_path"), str) and Path(str(item["local_path"])) == destination
            for item in artwork
        )
        if not already:
            artwork.append(
                {
                    "url": f"file://{destination}",
                    "kind": kind,
                    "local_path": str(destination),
                }
            )
            changed = True
    if changed:
        work.artwork = artwork
        repository._session.flush()
