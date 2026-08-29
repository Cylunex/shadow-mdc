from datetime import date

import pytest

from shadow_mdc.domain import IdentityHints, ProviderRecord
from shadow_mdc.enums import ContentFamily, MatchDecision, QueryMode
from shadow_mdc.matching import rank_candidates, score_candidate


def record(
    *,
    external_id: str = "one",
    code: str | None = None,
    title: str = "A title",
    runtime_seconds: int | None = None,
    fingerprints: dict[str, str] | None = None,
) -> ProviderRecord:
    return ProviderRecord(
        provider="fixture",
        external_id=external_id,
        code=code,
        title=title,
        family=ContentFamily.JAV,
        release_date=date(2025, 1, 2),
        runtime_seconds=runtime_seconds,
        fingerprints=fingerprints or {},
    )


@pytest.mark.parametrize(
    ("hints", "candidate", "decision"),
    [
        (
            IdentityHints(term="SSIS-123", mode=QueryMode.CODE, family=ContentFamily.JAV, code="SSIS-123"),
            record(code="ssis_123"),
            MatchDecision.ACCEPT,
        ),
        (
            IdentityHints(term="A title", mode=QueryMode.TEXT, title="A title"),
            record(title="A title"),
            MatchDecision.REVIEW,
        ),
        (
            IdentityHints(term="Unrelated", mode=QueryMode.TEXT, title="Unrelated"),
            record(title="Completely different"),
            MatchDecision.REJECT,
        ),
    ],
)
def test_score_decisions(
    hints: IdentityHints,
    candidate: ProviderRecord,
    decision: MatchDecision,
) -> None:
    assert score_candidate(hints, candidate).decision is decision


def test_fingerprint_is_deterministic() -> None:
    hints = IdentityHints(
        term="0123456789abcdef",
        mode=QueryMode.FINGERPRINT,
        fingerprints={"oshash": "0123456789abcdef"},
    )

    result = score_candidate(hints, record(fingerprints={"oshash": "0123456789ABCDEF"}))

    assert result.score == 1
    assert result.decision is MatchDecision.ACCEPT
    assert result.evidence[0].kind == "fingerprint"


def test_ranking_is_stable_for_equal_scores() -> None:
    hints = IdentityHints(term="unknown", mode=QueryMode.TEXT, title="unknown")
    candidates = [
        record(external_id="b", title="different"),
        record(external_id="a", title="different"),
    ]

    ranked = rank_candidates(hints, candidates)

    assert [item.record.external_id for item in ranked] == ["a", "b"]
