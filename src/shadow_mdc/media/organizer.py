import hashlib
import hmac
import json
import os
import re
import shutil
import tempfile
from collections.abc import Iterable
from pathlib import Path

from ..db.models import ExternalIdentity, Library, MediaAsset, Work
from ..db.repository import Repository
from ..domain import FileOperation, OperationPlan
from ..enums import NfoPolicy, OperationKind, OutputMode
from ..services.path_filter import MediaPathFilter
from .nfo import build_nfo, write_nfo
from .parts import detect_media_part, find_subtitles, subtitle_destination

_INVALID_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_PORTABLE_PATH_LIMIT = 240


class Organizer:
    def __init__(self, repository: Repository):
        self._repository = repository

    def plan(
        self,
        *,
        asset: MediaAsset,
        work: Work,
        library: Library,
        mode: OutputMode,
        target_root: str | None = None,
        template: str | None = None,
        media_suffix: str | None = None,
        extra_operations: tuple[FileOperation, ...] = (),
    ) -> OperationPlan:
        source = Path(asset.path).resolve()
        operations: list[FileOperation] = []

        if mode is OutputMode.SIDECAR:
            destination = source
        else:
            root = _target_root(target_root)
            selected_template = template or library.organize_template
            rendered = _render_template(selected_template, asset=asset, work=work)
            if Path(rendered).is_absolute():
                raise ValueError("organize template must be relative")
            relative_destination = Path(rendered)
            part = detect_media_part(source, work.primary_code)
            if part is not None and "{part}" not in selected_template:
                relative_destination = relative_destination.with_name(
                    f"{relative_destination.stem}{part.suffix}{relative_destination.suffix}"
                )
            if media_suffix:
                if not re.fullmatch(r"_[A-Za-z0-9-]+", media_suffix):
                    raise ValueError("media suffix contains unsupported characters")
                relative_destination = relative_destination.with_name(
                    f"{relative_destination.stem}{media_suffix}{relative_destination.suffix}"
                )
            relative_destination = _fit_relative_path(
                root,
                relative_destination,
                max_path_units=_PORTABLE_PATH_LIMIT,
            )
            destination = (root / relative_destination).resolve()
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise ValueError("organize destination escapes target root") from exc
            operation_kind = OperationKind(mode.value)
            if not _same_file(source, destination):
                operations.append(
                    FileOperation(
                        kind=operation_kind,
                        source=str(source),
                        destination=str(destination),
                        conflict=destination.exists(),
                        detail="media",
                    )
                )
            for subtitle in find_subtitles(source, work.primary_code):
                subtitle_target = subtitle_destination(subtitle, source, destination)
                if _same_file(subtitle.resolve(), subtitle_target):
                    continue
                operations.append(
                    FileOperation(
                        kind=operation_kind,
                        source=str(subtitle.resolve()),
                        destination=str(subtitle_target),
                        conflict=subtitle_target.exists(),
                        detail="subtitle",
                    )
                )
        operations.extend(_artwork_operations(work, destination.parent))
        nfo_path = destination.parent / "movie.nfo"
        operations.append(
            FileOperation(
                kind=OperationKind.WRITE_NFO,
                destination=str(nfo_path),
                conflict=nfo_path.exists(),
                detail="NFO is replaced atomically after token verification",
            )
        )
        operations.extend(extra_operations)
        token = _plan_token(asset.id, operations)
        return OperationPlan(asset_id=asset.id, token=token, operations=tuple(operations))

    def execute(
        self,
        *,
        asset: MediaAsset,
        work: Work,
        library: Library,
        identities: list[ExternalIdentity],
        mode: OutputMode,
        token: str,
        target_root: str | None = None,
        template: str | None = None,
        nfo_policy: NfoPolicy = NfoPolicy.ERROR,
        media_suffix: str | None = None,
        extra_operations: tuple[FileOperation, ...] = (),
    ) -> OperationPlan:
        plan = self.plan(
            asset=asset,
            work=work,
            library=library,
            mode=mode,
            target_root=target_root,
            template=template,
            media_suffix=media_suffix,
            extra_operations=extra_operations,
        )
        if not hmac.compare_digest(plan.token, token):
            raise ValueError("operation plan changed; request a new plan")
        blocking_conflicts = [
            operation
            for operation in plan.operations
            if operation.conflict
            and not (operation.detail or "").startswith("artwork:")
            and (operation.kind is not OperationKind.WRITE_NFO or nfo_policy is NfoPolicy.ERROR)
        ]
        if blocking_conflicts:
            raise FileExistsError(blocking_conflicts[0].destination)

        for operation in plan.operations:
            destination = Path(operation.destination)
            if operation.kind is OperationKind.DELETE_FILTERED_FILE:
                if destination.is_file() and not destination.is_symlink():
                    destination.unlink()
                continue
            if operation.kind is OperationKind.REMOVE_DIRECTORY:
                try:
                    destination.rmdir()
                except FileNotFoundError:
                    pass
                except OSError:
                    # A failed earlier move or a file created after preview keeps the directory safe.
                    pass
                continue
            if operation.conflict and (operation.detail or "").startswith("artwork:"):
                continue
            if operation.kind is OperationKind.WRITE_NFO:
                if operation.conflict and nfo_policy is NfoPolicy.SKIP:
                    continue
                write_nfo(destination, build_nfo(work, identities, asset))
                continue
            if operation.source is None:
                raise ValueError("media operation has no source")
            source = Path(operation.source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if (operation.detail or "").startswith("artwork:"):
                _write_artwork(source, destination, operation.detail or "artwork:fanart")
                continue
            if operation.kind is OperationKind.MOVE:
                shutil.move(str(source), str(destination))
                if operation.detail == "media":
                    self._repository.update_asset_path(asset, str(destination))
            elif operation.kind is OperationKind.COPY:
                shutil.copy2(source, destination)
            elif operation.kind is OperationKind.HARDLINK:
                os.link(source, destination)
            elif operation.kind is OperationKind.SYMLINK:
                os.symlink(source, destination)
        return plan


def plan_move_cleanup(
    library: Library,
    operations: Iterable[FileOperation],
    path_filter: MediaPathFilter,
) -> tuple[FileOperation, ...]:
    """Plan filtered-file and empty-directory cleanup after a set of moves."""
    root = Path(library.root_path).resolve()
    moved_sources = {
        Path(operation.source).resolve()
        for operation in operations
        if operation.kind is OperationKind.MOVE and operation.source is not None
    }
    branches: set[Path] = set()
    for source in moved_sources:
        try:
            relative = source.relative_to(root)
        except ValueError:
            continue
        branches.add(root if len(relative.parts) == 1 else root / relative.parts[0])

    planned: list[FileOperation] = []
    emitted: set[tuple[OperationKind, str]] = set()
    for branch in sorted(branches, key=lambda item: str(item).casefold()):
        removable, branch_operations = _cleanup_branch_plan(
            branch,
            root=root,
            moved_sources=moved_sources,
            path_filter=path_filter,
        )
        if removable and branch != root:
            branch_operations.append(_remove_directory_operation(branch))
        for operation in branch_operations:
            key = (operation.kind, operation.destination.casefold())
            if key not in emitted:
                planned.append(operation)
                emitted.add(key)
    return tuple(planned)


def _cleanup_branch_plan(
    directory: Path,
    *,
    root: Path,
    moved_sources: set[Path],
    path_filter: MediaPathFilter,
) -> tuple[bool, list[FileOperation]]:
    try:
        with os.scandir(directory) as iterator:
            entries = sorted(iterator, key=lambda item: item.name.casefold())
    except OSError:
        return False, []

    has_content = False
    nested_operations: list[FileOperation] = []
    filtered_files: list[FileOperation] = []
    for entry in entries:
        path = Path(entry.path)
        try:
            if entry.is_symlink():
                has_content = True
            elif entry.is_file(follow_symlinks=False):
                resolved = path.resolve()
                if resolved in moved_sources:
                    continue
                match = path_filter.match(resolved, root)
                if match is None:
                    has_content = True
                else:
                    filtered_files.append(
                        FileOperation(
                            kind=OperationKind.DELETE_FILTERED_FILE,
                            destination=str(resolved),
                            detail=f"cleanup:filtered:{match.word}",
                        )
                    )
            elif entry.is_dir(follow_symlinks=False):
                removable, child_operations = _cleanup_branch_plan(
                    path,
                    root=root,
                    moved_sources=moved_sources,
                    path_filter=path_filter,
                )
                nested_operations.extend(child_operations)
                if removable:
                    nested_operations.append(_remove_directory_operation(path.resolve()))
                else:
                    has_content = True
            else:
                has_content = True
        except OSError:
            has_content = True

    if has_content:
        return False, nested_operations
    return True, [*nested_operations, *filtered_files]


def _remove_directory_operation(path: Path) -> FileOperation:
    return FileOperation(
        kind=OperationKind.REMOVE_DIRECTORY,
        destination=str(path),
        detail="cleanup:empty-directory",
    )


def _render_template(template: str, *, asset: MediaAsset, work: Work) -> str:
    source = Path(asset.path)
    part = detect_media_part(source, work.primary_code)
    display_title = _title_without_code(work.title, work.primary_code)
    group = _group_name(work)
    subgroup = _subgroup_name(work)
    folder_name = f"[{work.primary_code}] {display_title}" if work.primary_code else display_title
    media_name = work.primary_code or display_title
    values = {
        "studio": _safe(work.studio or "Unknown Studio"),
        "code": _safe(work.primary_code or ""),
        "title": _safe(display_title),
        "code_or_title": _safe(work.primary_code or work.title),
        "family": _safe(work.family),
        "category": _safe(work.category),
        "actor": _safe(work.actors[0] if work.actors else (work.studio or work.series or "Unknown Actor")),
        "group": _safe(group),
        "subgroup": _safe(subgroup),
        "folder_name": _safe(folder_name),
        "media_name": _safe(media_name),
        "part": part.suffix if part else "",
        "year": str(work.release_date.year) if work.release_date else "Unknown Year",
        "ext": source.suffix.lstrip(".").casefold(),
    }
    try:
        rendered = template.format_map(values)
    except KeyError as exc:
        raise ValueError(f"unknown organize template field: {exc.args[0]}") from exc
    if not rendered.strip():
        raise ValueError("organize template rendered empty")
    return rendered


def _safe(value: str) -> str:
    cleaned = _INVALID_SEGMENT.sub("_", value).strip(" .")
    return cleaned[:180] or "Unknown"


def _fit_relative_path(root: Path, relative: Path, *, max_path_units: int) -> Path:
    """Shorten long path segments deterministically for portable Windows output."""
    parts = list(relative.parts)
    if not parts:
        raise ValueError("organize template rendered empty")
    root_units = _utf16_units(str(root))
    separator_units = len(parts)
    available_units = max_path_units - root_units - separator_units
    minimums = [min(_utf16_units(part), 12) for part in parts]
    minimums[-1] = min(_utf16_units(parts[-1]), 20)
    if sum(minimums) > available_units:
        raise ValueError("target root is too long for a portable organize path")

    while sum(_utf16_units(part) for part in parts) > available_units:
        candidates = [
            (_utf16_units(part) - minimums[index], index)
            for index, part in enumerate(parts)
            if _utf16_units(part) > minimums[index]
        ]
        reducible, index = max(candidates, default=(0, -1))
        if reducible <= 0:
            raise ValueError("organize path cannot be shortened safely")
        overflow = sum(_utf16_units(part) for part in parts) - available_units
        target_units = _utf16_units(parts[index]) - min(overflow, reducible)
        parts[index] = _shorten_segment(
            parts[index],
            target_units,
            preserve_extension=index == len(parts) - 1,
        )
    return Path(*parts)


def _shorten_segment(value: str, max_units: int, *, preserve_extension: bool) -> str:
    if _utf16_units(value) <= max_units:
        return value
    extension = Path(value).suffix if preserve_extension else ""
    stem = value[: -len(extension)] if extension else value
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:8]
    marker = f"~{digest}"
    prefix_units = max_units - _utf16_units(marker) - _utf16_units(extension)
    if prefix_units < 1:
        raise ValueError("organize path segment cannot be shortened safely")
    return f"{_truncate_utf16(stem, prefix_units).rstrip(' .')}{marker}{extension}"


def _truncate_utf16(value: str, max_units: int) -> str:
    result: list[str] = []
    used = 0
    for character in value:
        units = _utf16_units(character)
        if used + units > max_units:
            break
        result.append(character)
        used += units
    return "".join(result)


def _utf16_units(value: str) -> int:
    return len(value.encode("utf-16-le")) // 2


def _same_file(source: Path, destination: Path) -> bool:
    if source == destination:
        return True
    try:
        return source.samefile(destination)
    except OSError:
        return False


def _target_root(value: str | None) -> Path:
    if not value or not value.strip():
        raise ValueError("target_root is required for copy, move, hardlink and symlink modes")
    root = Path(value).resolve()
    if not root.is_dir():
        raise ValueError("target_root must be an existing directory")
    return root


def _artwork_operations(work: Work, destination_dir: Path) -> list[FileOperation]:
    operations: list[FileOperation] = []
    sources: dict[str, Path] = {}
    for item in work.artwork:
        raw_path = item.get("local_path")
        if not isinstance(raw_path, str):
            continue
        source = Path(raw_path).resolve()
        if not source.is_file():
            continue
        raw_kind = str(item.get("kind", "thumb")).casefold()
        kind = "fanart" if raw_kind in {"fanart", "background", "backdrop"} else "poster"
        sources.setdefault(kind, source)
    if not sources:
        return operations
    sources.setdefault("fanart", sources.get("poster", next(iter(sources.values()))))
    sources.setdefault("poster", sources.get("fanart", next(iter(sources.values()))))
    for kind in ("fanart", "poster"):
        source = sources[kind]
        extension = ".jpg" if source.suffix.casefold() in {".jpg", ".jpeg"} else source.suffix.casefold()
        target = destination_dir / f"{kind}{extension}"
        if _same_file(source, target):
            continue
        operations.append(
            FileOperation(
                kind=OperationKind.COPY,
                source=str(source),
                destination=str(target),
                conflict=target.exists(),
                detail=f"artwork:{kind}",
            )
        )
    return operations


def _write_artwork(source: Path, destination: Path, detail: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=destination.name,
        suffix=".tmp",
        dir=destination.parent,
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _title_without_code(title: str, code: str | None) -> str:
    if not code:
        return title.strip()
    pattern = re.compile(rf"(?i)^\s*[\[【]?{re.escape(code)}[\]】]?\s*[-\u2013\u2014:\uFF1A]?\s*")
    cleaned = pattern.sub("", title, count=1).strip()
    return cleaned or code


def _group_name(work: Work) -> str:
    return {
        "Japan": "JAV",
        "China": "国产",
        "Korea": "韩国",
        "Europe": "欧美",
        "Other": "其他",
    }.get(work.category, "其他")


def _subgroup_name(work: Work) -> str:
    text = " ".join((work.primary_code or "", work.title, *work.tags)).casefold()
    if work.category == "Japan":
        if (work.primary_code or "").upper().startswith("FC2-"):
            return "FC2"
        uncensored = ("无码", "無碼", "uncensored", "heyzo", "1pondo", "carib", "10musume")
        return "无码" if any(marker in text for marker in uncensored) else "有码"
    if work.category == "China":
        for marker, name in (("麻豆", "麻豆"), ("探花", "探花"), ("91", "91"), ("自拍", "自拍")):
            if marker in text:
                return name
        return "其他"
    return "影片"


def _plan_token(asset_id: str, operations: list[FileOperation]) -> str:
    payload = {
        "asset_id": asset_id,
        "operations": [operation.model_dump(mode="json", exclude={"conflict"}) for operation in operations],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
