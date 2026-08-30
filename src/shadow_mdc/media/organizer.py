import hashlib
import hmac
import json
import os
import re
import shutil
from pathlib import Path

from ..db.models import ExternalIdentity, Library, MediaAsset, Work
from ..db.repository import Repository
from ..domain import FileOperation, OperationPlan
from ..enums import NfoPolicy, OperationKind, OutputMode
from .nfo import build_nfo, write_nfo
from .parts import detect_media_part, find_subtitles, subtitle_destination

_INVALID_SEGMENT = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


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
            destination = (root / rendered).resolve()
            try:
                destination.relative_to(root)
            except ValueError as exc:
                raise ValueError("organize destination escapes target root") from exc
            part = detect_media_part(source, work.primary_code)
            if part is not None and "{part}" not in selected_template:
                destination = destination.with_name(
                    f"{destination.stem}{part.suffix}{destination.suffix}"
                )
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
        operations.extend(_artwork_operations(work, destination))
        nfo_path = destination.with_suffix(".nfo")
        operations.append(
            FileOperation(
                kind=OperationKind.WRITE_NFO,
                destination=str(nfo_path),
                conflict=nfo_path.exists(),
                detail="NFO is replaced atomically after token verification",
            )
        )
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
    ) -> OperationPlan:
        plan = self.plan(
            asset=asset,
            work=work,
            library=library,
            mode=mode,
            target_root=target_root,
            template=template,
        )
        if not hmac.compare_digest(plan.token, token):
            raise ValueError("operation plan changed; request a new plan")
        blocking_conflicts = [
            operation
            for operation in plan.operations
            if operation.conflict
            and operation.detail != "artwork"
            and (operation.kind is not OperationKind.WRITE_NFO or nfo_policy is NfoPolicy.ERROR)
        ]
        if blocking_conflicts:
            raise FileExistsError(blocking_conflicts[0].destination)

        for operation in plan.operations:
            destination = Path(operation.destination)
            if operation.conflict and operation.detail == "artwork":
                continue
            if operation.kind is OperationKind.WRITE_NFO:
                if operation.conflict and nfo_policy is NfoPolicy.SKIP:
                    continue
                write_nfo(destination, build_nfo(work, identities))
                continue
            if operation.source is None:
                raise ValueError("media operation has no source")
            source = Path(operation.source)
            destination.parent.mkdir(parents=True, exist_ok=True)
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


def _render_template(template: str, *, asset: MediaAsset, work: Work) -> str:
    source = Path(asset.path)
    part = detect_media_part(source, work.primary_code)
    values = {
        "studio": _safe(work.studio or "Unknown Studio"),
        "code": _safe(work.primary_code or ""),
        "title": _safe(work.title),
        "code_or_title": _safe(work.primary_code or work.title),
        "family": _safe(work.family),
        "category": _safe(work.category),
        "actor": _safe(work.actors[0] if work.actors else "Unknown Actor"),
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


def _artwork_operations(work: Work, destination: Path) -> list[FileOperation]:
    operations: list[FileOperation] = []
    emitted: set[str] = set()
    for item in work.artwork:
        raw_path = item.get("local_path")
        if not isinstance(raw_path, str):
            continue
        source = Path(raw_path).resolve()
        if not source.is_file():
            continue
        raw_kind = str(item.get("kind", "thumb")).casefold()
        kind = "fanart" if raw_kind == "fanart" else "poster"
        if kind in emitted:
            continue
        emitted.add(kind)
        target = destination.with_name(f"{destination.stem}-{kind}{source.suffix.casefold()}")
        if _same_file(source, target):
            continue
        operations.append(
            FileOperation(
                kind=OperationKind.COPY,
                source=str(source),
                destination=str(target),
                conflict=target.exists(),
                detail="artwork",
            )
        )
    return operations


def _plan_token(asset_id: str, operations: list[FileOperation]) -> str:
    payload = {
        "asset_id": asset_id,
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
