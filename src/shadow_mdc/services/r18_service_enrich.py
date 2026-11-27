"""Update existing catalog gaps from bounded R18 requests."""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from itertools import batched

from sqlalchemy import select, text

from ..db.models import Work
from ..db.repository import Database, Repository, missing_provider_fields
from ..enums import ContentFamily
from ..identity import extract_code
from ..providers.base import ProviderError
from ..providers.r18_dump import R18_BATCH_LIMIT, R18ServiceProvider


@dataclass(frozen=True)
class _Target:
    work_id: str
    primary_code: str
    query: str


@dataclass
class R18ServiceEnrichOutcome:
    candidates: int = 0
    matched: int = 0
    not_found: int = 0
    updated: int = 0
    skipped: int = 0
    unsupported_codes: int = 0
    conflicts: int = 0
    batches_completed: int = 0
    filled: dict[str, int] = field(default_factory=dict)
    error: str | None = None


async def enrich_existing_jav_from_service(
    database: Database,
    provider: R18ServiceProvider,
    *,
    codes: frozenset[str] = frozenset(),
    year: int | None = None,
    limit: int = 0,
    offset: int = 0,
    dry_run: bool = False,
    commit_every: int = 50,
) -> R18ServiceEnrichOutcome:
    """Fetch records outside catalog transactions and commit short update-only groups."""
    if limit < 0 or offset < 0 or commit_every < 1:
        raise ValueError("limit and offset must be nonnegative and commit_every must be positive")
    outcome = R18ServiceEnrichOutcome()
    with database.session() as session:
        works = session.scalars(
            select(Work).where(Work.family == ContentFamily.JAV.value, Work.primary_code.is_not(None))
            .order_by(Work.created_at.desc(), Work.id)
            .execution_options(yield_per=256)
        )
        ranked: list[tuple[int, _Target]] = []
        for work in works:
            primary_code = work.primary_code
            if not primary_code or not missing_provider_fields(work):
                continue
            query, family = extract_code(primary_code)
            if query is None or family is not ContentFamily.JAV or query.startswith("FC2-"):
                if not codes:
                    outcome.unsupported_codes += 1
                continue
            if codes and query not in codes:
                continue
            rank = 0
            if year is not None:
                if work.release_date is None:
                    rank = 1
                elif work.release_date.year != year:
                    rank = 2
            ranked.append((rank, _Target(work.id, primary_code, query)))
            if year is None and limit and len(ranked) == offset + limit:
                break
        ranked.sort(key=lambda item: item[0])
        targets = [target for _, target in ranked[offset : offset + limit if limit else None]]

    outcome.candidates = len(targets)
    filled: Counter[str] = Counter()
    for batch in batched(targets, R18_BATCH_LIMIT):
        queries = tuple(dict.fromkeys(item.query for item in batch))
        try:
            results = await provider.search_batch(queries)
        except ProviderError as exc:
            outcome.error = f"{exc.reason}: {exc.detail}"
            break

        by_query = dict(zip(queries, results, strict=True))
        hits = []
        for item in batch:
            match = by_query[item.query]
            if match.identity_conflict:
                outcome.conflicts += 1
            elif match.record is None:
                outcome.not_found += 1
            else:
                hits.append((item, match.record))
        outcome.matched += len(hits)
        if not dry_run:
            for group in batched(hits, commit_every):
                with database.session() as session:
                    # Serialize the read/merge with concurrent SQLite edits only after HTTP finishes.
                    if database.engine.dialect.name == "sqlite":
                        session.execute(text("BEGIN IMMEDIATE"))
                    repo = Repository(session)
                    for target, record in group:
                        work = session.get(Work, target.work_id, with_for_update=True)
                        if (
                            work is None or work.primary_code != target.primary_code
                            or work.family != ContentFamily.JAV.value
                            or not (before := missing_provider_fields(work))
                        ):
                            outcome.skipped += 1
                            continue
                        if not repo.can_merge_provider_record(work, record):
                            outcome.conflicts += 1
                            continue
                        repo.merge_provider_into_work(work, record, only_missing=True)
                        newly_filled = before - missing_provider_fields(work)
                        if newly_filled:
                            outcome.updated += 1
                            filled.update(newly_filled)
        outcome.batches_completed += 1

    outcome.filled = dict(filled)
    return outcome
