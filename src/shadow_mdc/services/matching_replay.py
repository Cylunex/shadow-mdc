"""Replay manually confirmed candidates against the current matcher."""

from __future__ import annotations

import hashlib

from pydantic import BaseModel, ValidationError

from ..db.models import MatchCandidateRow, MatchFeedback
from ..domain import IdentityHints, ProviderRecord
from ..enums import MatchDecision
from ..identity import normalize_identity_value
from ..matching import rank_candidates

_HINT_FIELDS = {
    "term", "mode", "code", "title", "source_url", "external_ids", "fingerprints",
    "duration_seconds", "studio", "series", "actors",
}
_RECORD_FIELDS = {
    "provider", "external_id", "source_url", "code", "title", "runtime_seconds",
    "studio", "series", "actors", "fingerprints",
}
_LOCAL_PROVIDERS = {"local-path", "local-manual"}


def _url_key(value: str | None) -> str | None:
    return hashlib.sha256(value.rstrip("/").encode()).hexdigest() if value else None


def capture_feedback(
    raw_hints: dict[str, object], chosen: MatchCandidateRow, candidates: list[MatchCandidateRow],
) -> MatchFeedback | None:
    try:
        hints = IdentityHints.model_validate(raw_hints)
    except ValidationError:
        return None
    records = []
    for row in candidates:
        if row.provider in _LOCAL_PROVIDERS:
            continue
        try:
            records.append(ProviderRecord.model_validate(row.record))
        except ValidationError:
            continue
    if not records:
        return None
    hint_data = hints.model_dump(mode="json", include=_HINT_FIELDS)
    hint_data["source_url"] = _url_key(hints.source_url)
    if hints.source_url and hints.term == hints.source_url:
        hint_data["term"] = hint_data["source_url"]
    record_data: list[dict[str, object]] = []
    for record in records:
        data = record.model_dump(mode="json", include=_RECORD_FIELDS)
        data["source_url"] = _url_key(record.source_url)
        record_data.append(data)
    return MatchFeedback(
        asset_id=chosen.asset_id, hints=hint_data, candidates=record_data,
        chosen_provider=chosen.provider, chosen_external_id=chosen.external_id,
        chosen_code=ProviderRecord.model_validate(chosen.record).code,
    )


class ReplayCounts(BaseModel):
    samples: int = 0
    manual_review: int = 0
    automatic: int = 0
    disagreed: int = 0


class MatchingReplay(BaseModel):
    code: ReplayCounts
    without_code: ReplayCounts


def replay_feedback(samples: list[MatchFeedback]) -> MatchingReplay:
    report = MatchingReplay(code=ReplayCounts(), without_code=ReplayCounts())
    for sample in samples:
        hints = IdentityHints.model_validate(sample.hints)
        counts = report.code if hints.code else report.without_code
        counts.samples += 1
        ranked = rank_candidates(hints, [ProviderRecord.model_validate(item) for item in sample.candidates])
        if not ranked or ranked[0].decision != MatchDecision.ACCEPT:
            counts.manual_review += 1
            continue
        counts.automatic += 1
        winner = ranked[0].record
        same_record = (winner.provider, winner.external_id) == (
            sample.chosen_provider, sample.chosen_external_id,
        )
        same_code = bool(
            winner.code and sample.chosen_code
            and normalize_identity_value(winner.code) == normalize_identity_value(sample.chosen_code)
        )
        counts.disagreed += int(not same_record and not same_code)
    return report
