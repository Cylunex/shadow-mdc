"""Restart handling for long-running task runs.

When the process dies mid-job, its ``TaskRun`` rows stay unfinished forever.
On startup we close them as ``interrupted`` and tag whether they can be resumed
through ``POST /api/tasks/{id}/resume``. Resuming scan/identify is safe because
both are idempotent with their incremental flags (``only_new`` /
``skip_identified``): already-finished assets are skipped, so the re-run
effectively continues where the previous one stopped.
"""

from __future__ import annotations

from ..db.repository import Database, Repository

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
