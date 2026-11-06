"""Close interrupted legacy tasks and preserve queued library jobs for startup recovery."""

from __future__ import annotations

from ..db.repository import Database, Repository
from .library_jobs import library_checkpoint

INTERRUPTED = "interrupted"
RESUMABLE_KINDS: frozenset[str] = frozenset({"scan", "identify:library", "subtitles:backfill"})


def is_resumable_kind(kind: str) -> bool:
    return kind in RESUMABLE_KINDS


def mark_interrupted_task_runs(database: Database) -> list[str]:
    """Close every unfinished task run left behind by a previous process."""

    closed: list[str] = []
    with database.session() as session:
        repo = Repository(session)
        for task in repo.list_unfinished_task_runs():
            checkpoint = library_checkpoint(session, task.id)
            if checkpoint is not None and checkpoint.status in {"queued", "running"}:
                continue
            summary = dict(task.summary or {})
            summary["interrupted"] = True
            summary["resumable"] = is_resumable_kind(task.kind)
            repo.finish_task_run(
                task,
                status=INTERRUPTED,
                summary=summary,
                error="process restarted before the task finished",
            )
            closed.append(task.id)
    return closed
