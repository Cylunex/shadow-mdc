"""Persisted inputs and item checkpoints for library scan and identify tasks."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..api_models import BulkIdentifyRequest, ScanRequest
from ..db.models import JobCheckpoint, Library, LibraryScanSchedule, TaskRun

LIBRARY_TASK_KINDS = ("scan", "identify:library")
CHECKPOINT_PREFIX = "library-run:"


class LibraryJobState(BaseModel):
    library_id: str
    operation: Literal["scan", "identify"]
    scan: ScanRequest = Field(default_factory=ScanRequest)
    identify: BulkIdentifyRequest = Field(default_factory=BulkIdentifyRequest)
    only_asset_ids: set[str] | None = None
    completed_asset_ids: set[str] = Field(default_factory=set)
    failed_asset_ids: set[str] = Field(default_factory=set)
    queried_groups: int = 0
    attempted: int = 0
    catalog_reused: int = 0
    online_identified: int = 0
    local_optimized: int = 0
    code_queries: int = 0
    provider_failures: int = 0
    pending_work_ids: set[str] = Field(default_factory=set)


class ScanSchedule(BaseModel):
    interval_hours: int = Field(default=0, ge=0, le=168)


def due_scans(session: Session, now: datetime) -> list[str]:
    due = []
    for library, schedule in session.execute(select(Library, LibraryScanSchedule).join(
        LibraryScanSchedule, Library.id == LibraryScanSchedule.library_id,
    )):
        last_scan = session.scalar(select(TaskRun.created_at).where(
            TaskRun.kind == "scan", TaskRun.scope == library.root_path,
        ).order_by(TaskRun.created_at.desc()).limit(1))
        last = max(schedule.enabled_at.replace(tzinfo=UTC),
                   last_scan.replace(tzinfo=UTC) if last_scan else datetime.min.replace(tzinfo=UTC))
        if now >= last + timedelta(hours=schedule.interval_hours):
            due.append(library.id)
    return due


class LibraryWorkerStopped(Exception):
    """Leave the persisted run unfinished so it can continue on startup."""


def active_library_task(session: Session, root_path: str) -> TaskRun | None:
    """Serialize admissions before checking the active runs for the same library."""
    session.execute(text("BEGIN IMMEDIATE"))
    return session.scalars(select(TaskRun).where(
        TaskRun.scope == root_path,
        TaskRun.kind.in_(LIBRARY_TASK_KINDS),
        TaskRun.finished_at.is_(None),
    ).order_by(TaskRun.created_at.desc())).first()


def library_checkpoint(session: Session, task_id: str) -> JobCheckpoint | None:
    return session.get(JobCheckpoint, CHECKPOINT_PREFIX + task_id)
