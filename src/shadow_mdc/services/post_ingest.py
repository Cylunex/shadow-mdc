"""Shared post-ingest hook: translate freshly ingested works (title/plot → zh).

Every ingest path (API discover seed, seed_* scripts, catalog import, MCP offline
add) should call into here so bilingual metadata does not depend on which entry
point created the work. Translation failures never propagate to the caller.
"""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Iterable
from dataclasses import asdict, dataclass, field
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import httpx
from sqlalchemy import or_, select

from ..db.models import Work
from .translation import (
    GoogleTitleTranslator,
    TranslationCache,
    build_translation_backends,
    needs_translation,
)

if TYPE_CHECKING:
    from ..config import Settings
    from ..db.repository import Database, Repository

log = logging.getLogger(__name__)


def build_work_translator(
    settings: "Settings", client: httpx.AsyncClient, *, enabled: bool | None = None
) -> GoogleTitleTranslator:
    backends = build_translation_backends(
        client,
        google_endpoint=settings.translation_endpoint,
        deepl_api_url=settings.translation_deepl_api_url,
        deepl_api_key=settings.translation_deepl_api_key or settings.translation_api_key,
        deeplx_endpoint=settings.translation_deeplx_endpoint,
        custom_endpoint=settings.translation_custom_endpoint,
        prefer=settings.translation_backend,
    )
    return GoogleTitleTranslator(
        client,
        TranslationCache(settings.data_dir / "translations.db"),
        enabled=settings.translation_enabled if enabled is None else enabled,
        endpoint=settings.translation_endpoint,
        target_language=settings.translation_target_language,
        extra_backends=[b for b in backends if b.name != "google"],
        translate_plot=settings.translation_plot,
    )


def work_needs_translation(work: Work) -> bool:
    sources = work.field_sources or {}
    for name in ("title", "plot"):
        src = str(sources.get(name, ""))
        if src.startswith("translation:") or src in {"local-manual", "local-path", "manual"}:
            continue
        if name in (work.field_locks or []):
            continue
        value = (work.title if name == "title" else work.plot) or ""
        if value.strip() and needs_translation(value, work.family):
            return True
    return False


async def translate_after_ingest(
    repo: "Repository", work: Work | None, translator: Any, *, timeout: float = 30.0
) -> str:
    """Translate one work; returns a status string, never raises."""

    if work is None or translator is None:
        return "skipped"
    try:
        result = await asyncio.wait_for(translator.translate_work(repo, work), timeout=timeout)
        return str(getattr(result, "status", "done"))
    except Exception as exc:  # noqa: BLE001 - ingest must not break on translation
        log.warning("post-ingest translation failed for %s: %s", getattr(work, "id", "?"), exc)
        return "failed"


@dataclass
class TranslationBackfillStats:
    scanned: int = 0
    translated: int = 0
    skipped: int = 0
    failed: int = 0
    remaining_estimate: int = 0
    last_id: str | None = None
    failed_ids: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["failed_ids"] = data["failed_ids"][:50]
        return data


def _candidate_ids(
    database: "Database",
    *,
    work_ids: Iterable[str] | None,
    since: datetime | None,
    after_id: str | None,
) -> list[str]:
    with database.session() as session:
        stmt = select(Work).order_by(Work.id)
        if work_ids is not None:
            ids = list(work_ids)
            if not ids:
                return []
            stmt = stmt.where(Work.id.in_(ids))
        if since is not None:
            stmt = stmt.where(or_(Work.updated_at >= since, Work.created_at >= since))
        if after_id:
            stmt = stmt.where(Work.id > after_id)
        return [w.id for w in session.scalars(stmt) if work_needs_translation(w)]


async def translate_pending_works(
    database: "Database",
    translator: Any,
    *,
    work_ids: Iterable[str] | None = None,
    since: datetime | None = None,
    limit: int | None = None,
    checkpoint: Path | None = None,
    timeout: float = 30.0,
) -> TranslationBackfillStats:
    """Translate works lacking translation. Bounded by ``limit``; resumable via ``checkpoint``."""

    from ..db.repository import Repository

    stats = TranslationBackfillStats()
    state: dict[str, Any] = {}
    if checkpoint is not None and checkpoint.is_file():
        try:
            state = json.loads(checkpoint.read_text())
        except (OSError, json.JSONDecodeError):
            state = {}
    after_id = state.get("last_id") if work_ids is None and since is None else None
    try:
        ids = _candidate_ids(database, work_ids=work_ids, since=since, after_id=after_id)
    except Exception as exc:  # noqa: BLE001
        log.warning("post-ingest translation scan failed: %s", exc)
        return stats
    batch = ids[:limit] if limit else ids
    stats.remaining_estimate = len(ids) - len(batch)
    for work_id in batch:
        stats.scanned += 1
        try:
            with database.session() as session:
                repo = Repository(session)
                status = await translate_after_ingest(
                    repo, repo.get_work(work_id), translator, timeout=timeout
                )
        except Exception as exc:  # noqa: BLE001
            log.warning("translation backfill commit failed for %s: %s", work_id, exc)
            status = "failed"
        if status == "translated":
            stats.translated += 1
        elif status == "failed":
            stats.failed += 1
            stats.failed_ids.append(work_id)
        else:
            stats.skipped += 1
        stats.last_id = work_id
        if checkpoint is not None and work_ids is None and since is None:
            checkpoint.parent.mkdir(parents=True, exist_ok=True)
            checkpoint.write_text(json.dumps({"last_id": work_id}))
    if checkpoint is not None and work_ids is None and since is None and stats.remaining_estimate == 0:
        checkpoint.unlink(missing_ok=True)  # full pass done → next run starts over
    return stats


async def run_post_ingest_translation(
    settings: "Settings",
    database: "Database",
    *,
    work_ids: Iterable[str] | None = None,
    since: datetime | None = None,
    client: httpx.AsyncClient | None = None,
) -> TranslationBackfillStats | None:
    """Script-friendly hook: build a translator, translate touched works, never raise."""

    if not settings.translation_enabled:
        return None
    own = client is None
    http = client or httpx.AsyncClient(
        timeout=settings.request_timeout_seconds, follow_redirects=True, proxy=settings.proxy_url
    )
    try:
        translator = build_work_translator(settings, http)
        return await translate_pending_works(database, translator, work_ids=work_ids, since=since)
    except Exception as exc:  # noqa: BLE001
        log.warning("post-ingest translation hook failed: %s", exc)
        return None
    finally:
        if own:
            await http.aclose()


def _argv_option(argv: list[str], name: str) -> str | None:
    for index, item in enumerate(argv):
        if item == name and index + 1 < len(argv):
            return argv[index + 1]
        if item.startswith(name + "="):
            return item.split("=", 1)[1]
    return None


def run_script_with_post_ingest(main_fn: Any, *, argv: list[str] | None = None) -> Any:
    """Run a seed script ``main`` then translate every work it created/updated.

    Disabled by ``--no-translate`` on argv or SHADOW_MDC_POST_INGEST_TRANSLATE=0.
    Translation errors are logged and never change the script's exit status.
    """

    import os
    import sys
    from datetime import UTC, timedelta

    args = list(sys.argv[1:] if argv is None else argv)
    disabled = "--no-translate" in args or os.environ.get("SHADOW_MDC_POST_INGEST_TRANSLATE", "1") == "0"
    if "--no-translate" in sys.argv:
        sys.argv.remove("--no-translate")
    started = datetime.now(UTC) - timedelta(seconds=2)
    outcome: Any = None
    exit_exc: BaseException | None = None
    try:
        outcome = main_fn()
    except SystemExit as exc:
        exit_exc = exc
    if not disabled and "--help" not in args and "-h" not in args and "--dry-run" not in args:
        try:
            from ..config import Settings
            from ..db.repository import Database

            settings = Settings()
            updates: dict[str, Any] = {}
            data_dir = _argv_option(args, "--data-dir")
            db_url = _argv_option(args, "--database-url")
            if data_dir:
                updates["data_dir"] = Path(data_dir)
            if db_url:
                updates["database_url"] = db_url
            if updates:
                settings = settings.model_copy(update=updates)
            database = Database(settings.database_url)
            stats = asyncio.run(run_post_ingest_translation(settings, database, since=started))
            if stats is not None:
                print("post-ingest translate: " + json.dumps(stats.as_dict(), ensure_ascii=False), file=sys.stderr)
        except Exception as exc:  # noqa: BLE001
            print(f"post-ingest translate failed: {exc}", file=sys.stderr)
    if exit_exc is not None:
        raise exit_exc
    return outcome
