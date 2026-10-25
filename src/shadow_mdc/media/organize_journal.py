"""Reverse-operation journal for organize runs, plus a guarded undo.

Every file mutation done by :meth:`Organizer.execute` can append a
:class:`JournalEntry` describing how to put things back. Undo replays the
entries in reverse order and refuses any step whose current filesystem state no
longer matches what the run left behind, so a user edit made after the run is
never clobbered.
"""

from __future__ import annotations

import os
import shutil
import uuid
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, Field

# NFOs are small; anything bigger is not backed up inline.
_MAX_INLINE_BACKUP_BYTES = 512 * 1024


class ReverseAction(StrEnum):
    MOVE_BACK = "move_back"  # move destination back to source
    DELETE_CREATED = "delete_created"  # remove a file/link the run created
    RESTORE_TEXT = "restore_text"  # restore previous file contents (NFO)
    RESTORE_TRASHED = "restore_trashed"  # move a trashed filtered file back
    RECREATE_DIRECTORY = "recreate_directory"  # mkdir a directory the run removed


class JournalEntry(BaseModel):
    action: ReverseAction
    path: str
    original: str | None = None
    previous_text: str | None = None
    size: int | None = None
    is_symlink: bool = False
    asset_id: str | None = None
    detail: str | None = None


class UndoStep(BaseModel):
    action: ReverseAction
    path: str
    original: str | None = None
    status: str  # done | would-do | skipped | error
    reason: str | None = None


class UndoReport(BaseModel):
    dry_run: bool
    steps: list[UndoStep] = Field(default_factory=list)
    done: int = 0
    skipped: int = 0
    errors: int = 0
    restored_assets: dict[str, str] = Field(default_factory=dict)


class OrganizeJournal:
    """Collects reverse operations while an organize run mutates the filesystem."""

    def __init__(self, trash_dir: Path | None = None) -> None:
        self.entries: list[JournalEntry] = []
        self.trash_dir = trash_dir

    def moved(self, source: Path, destination: Path, *, asset_id: str | None, detail: str | None) -> None:
        self.entries.append(
            JournalEntry(
                action=ReverseAction.MOVE_BACK,
                path=str(destination),
                original=str(source),
                asset_id=asset_id,
                detail=detail,
            )
        )

    def created(self, path: Path, *, detail: str | None) -> None:
        is_link = path.is_symlink()
        size = None if is_link or not path.is_file() else path.stat().st_size
        self.entries.append(
            JournalEntry(
                action=ReverseAction.DELETE_CREATED,
                path=str(path),
                size=size,
                is_symlink=is_link,
                detail=detail,
            )
        )

    def before_overwrite(self, path: Path, *, detail: str | None) -> bool:
        """Record prior text of ``path``; returns False when it cannot be backed up."""

        if not path.exists():
            return True
        try:
            if path.stat().st_size > _MAX_INLINE_BACKUP_BYTES:
                return False
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            return False
        self.entries.append(
            JournalEntry(action=ReverseAction.RESTORE_TEXT, path=str(path), previous_text=text, detail=detail)
        )
        return True

    def trash(self, path: Path) -> bool:
        """Move a filtered file into the trash instead of deleting it."""

        if self.trash_dir is None:
            return False
        self.trash_dir.mkdir(parents=True, exist_ok=True)
        target = self.trash_dir / f"{uuid.uuid4().hex}-{path.name}"
        shutil.move(str(path), str(target))
        self.entries.append(
            JournalEntry(action=ReverseAction.RESTORE_TRASHED, path=str(target), original=str(path))
        )
        return True

    def removed_directory(self, path: Path) -> None:
        self.entries.append(JournalEntry(action=ReverseAction.RECREATE_DIRECTORY, path=str(path)))

    def dump(self) -> list[dict[str, object]]:
        return [entry.model_dump(mode="json") for entry in self.entries]


def undo_entries(entries: list[JournalEntry], *, dry_run: bool) -> UndoReport:
    """Replay ``entries`` in reverse. Never overwrites a file that already exists."""

    report = UndoReport(dry_run=dry_run)
    for entry in reversed(entries):
        step = _undo_one(entry, dry_run=dry_run)
        report.steps.append(step)
        if step.status in {"done", "would-do"}:
            report.done += 1
            if entry.action is ReverseAction.MOVE_BACK and entry.detail == "media" and entry.asset_id:
                report.restored_assets[entry.asset_id] = entry.original or ""
        elif step.status == "error":
            report.errors += 1
        else:
            report.skipped += 1
    return report


def _undo_one(entry: JournalEntry, *, dry_run: bool) -> UndoStep:
    path = Path(entry.path)

    def step(status: str, reason: str | None = None) -> UndoStep:
        return UndoStep(
            action=entry.action, path=entry.path, original=entry.original, status=status, reason=reason
        )

    ok = "would-do" if dry_run else "done"
    try:
        if entry.action is ReverseAction.RECREATE_DIRECTORY:
            if path.is_dir():
                return step("skipped", "directory already exists")
            if not dry_run:
                path.mkdir(parents=True, exist_ok=True)
            return step(ok)
        if entry.action in {ReverseAction.MOVE_BACK, ReverseAction.RESTORE_TRASHED}:
            original = Path(entry.original or "")
            if not entry.original:
                return step("error", "journal entry has no original path")
            if not (path.exists() or path.is_symlink()):
                return step("skipped", "file no longer at organized location")
            if original.exists() or original.is_symlink():
                return step("skipped", "original location is occupied")
            if not dry_run:
                original.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(path), str(original))
            return step(ok)
        if entry.action is ReverseAction.DELETE_CREATED:
            if not (path.exists() or path.is_symlink()):
                return step("skipped", "already gone")
            if entry.is_symlink != path.is_symlink():
                return step("skipped", "file type changed since organize")
            if entry.size is not None and not path.is_symlink() and path.stat().st_size != entry.size:
                return step("skipped", "file changed since organize")
            if not dry_run:
                os.unlink(path)
            return step(ok)
        if entry.action is ReverseAction.RESTORE_TEXT:
            if entry.previous_text is None:
                return step("error", "no backup text")
            if not dry_run:
                path.parent.mkdir(parents=True, exist_ok=True)
                tmp = path.with_name(f".{path.name}.undo-{uuid.uuid4().hex}")
                tmp.write_text(entry.previous_text, encoding="utf-8")
                os.replace(tmp, path)
            return step(ok)
    except OSError as exc:
        return step("error", str(exc))
    return step("error", "unknown action")
