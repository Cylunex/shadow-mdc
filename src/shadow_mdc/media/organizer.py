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
from ..enums import OperationKind
from .nfo import build_nfo, write_nfo

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
        mode: OperationKind,
    ) -> OperationPlan:
        if mode not in {OperationKind.MOVE, OperationKind.COPY, OperationKind.HARDLINK}:
            raise ValueError(f"unsupported media operation: {mode}")
        source = Path(asset.path).resolve()
        root = Path(library.root_path).resolve()
        rendered = _render_template(library.organize_template, asset=asset, work=work)
        if Path(rendered).is_absolute():
            raise ValueError("organize template must be relative")
        destination = (root / rendered).resolve()
        try:
            destination.relative_to(root)
        except ValueError as exc:
            raise ValueError("organize destination escapes library root") from exc

        operations: list[FileOperation] = []
        same_file = _same_file(source, destination)
        if not same_file:
            operations.append(
                FileOperation(
                    kind=mode,
                    source=str(source),
                    destination=str(destination),
                    conflict=destination.exists(),
                )
            )
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
        mode: OperationKind,
        token: str,
        replace_nfo: bool = True,
    ) -> OperationPlan:
        plan = self.plan(asset=asset, work=work, library=library, mode=mode)
        if not hmac.compare_digest(plan.token, token):
            raise ValueError("operation plan changed; request a new plan")
        blocking_conflicts = [
            operation
            for operation in plan.operations
            if operation.conflict and (operation.kind is not OperationKind.WRITE_NFO or not replace_nfo)
        ]
        if blocking_conflicts:
            raise FileExistsError(blocking_conflicts[0].destination)

        destination_after_media = Path(asset.path)
        for operation in plan.operations:
            destination = Path(operation.destination)
            if operation.kind is OperationKind.WRITE_NFO:
                write_nfo(destination, build_nfo(work, identities))
                continue
            if operation.source is None:
                raise ValueError("media operation has no source")
            source = Path(operation.source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            if operation.kind is OperationKind.MOVE:
                shutil.move(str(source), str(destination))
                self._repository.update_asset_path(asset, str(destination))
            elif operation.kind is OperationKind.COPY:
                shutil.copy2(source, destination)
            elif operation.kind is OperationKind.HARDLINK:
                os.link(source, destination)
            destination_after_media = destination
        if mode is OperationKind.MOVE and destination_after_media != Path(asset.path):
            self._repository.update_asset_path(asset, str(destination_after_media))
        return plan


def _render_template(template: str, *, asset: MediaAsset, work: Work) -> str:
    source = Path(asset.path)
    values = {
        "studio": _safe(work.studio or "Unknown Studio"),
        "code": _safe(work.primary_code or ""),
        "title": _safe(work.title),
        "code_or_title": _safe(work.primary_code or work.title),
        "family": _safe(work.family),
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


def _plan_token(asset_id: str, operations: list[FileOperation]) -> str:
    payload = {
        "asset_id": asset_id,
        "operations": [operation.model_dump(mode="json") for operation in operations],
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()
