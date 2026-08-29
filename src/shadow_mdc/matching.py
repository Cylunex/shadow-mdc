import re
import unicodedata
from difflib import SequenceMatcher

from .domain import IdentityHints, MatchEvidence, ProviderRecord, ScoredCandidate
from .enums import MatchDecision
from .identity import normalize_identity_value

AUTO_ACCEPT_SCORE = 0.86
REVIEW_SCORE = 0.48

_TITLE_NOISE = re.compile(r"(?i)\b(?:4k|8k|1080p|2160p|uncensored|subtitle|中字|字幕)\b")
_TITLE_PUNCT = re.compile(r"[^\w\u3040-\u30ff\u3400-\u9fff]+")


def normalize_title(value: str) -> str:
    value = unicodedata.normalize("NFKC", value).casefold()
    value = _TITLE_NOISE.sub(" ", value)
    return " ".join(_TITLE_PUNCT.sub(" ", value).split())


def _title_similarity(left: str, right: str) -> float:
    a = normalize_title(left)
    b = normalize_title(right)
    if not a or not b:
        return 0
    if a == b:
        return 1
    return SequenceMatcher(a=a, b=b).ratio()


def _duration_similarity(expected: float, actual: int) -> float:
    if expected <= 0 or actual <= 0:
        return 0
    delta = abs(expected - actual)
    tolerance = max(120.0, expected * 0.08)
    return max(0.0, 1.0 - delta / tolerance)


def score_candidate(hints: IdentityHints, record: ProviderRecord) -> ScoredCandidate:
    evidence: list[MatchEvidence] = []

    for algorithm, value in hints.fingerprints.items():
        if normalize_identity_value(record.fingerprints.get(algorithm, "")) == normalize_identity_value(
            value
        ):
            evidence.append(MatchEvidence(kind="fingerprint", contribution=1, detail=f"{algorithm} exact"))
            return ScoredCandidate(
                record=record,
                score=1,
                decision=MatchDecision.ACCEPT,
                evidence=tuple(evidence),
            )

    known_external = hints.external_ids.get(record.provider)
    if known_external and normalize_identity_value(known_external) == normalize_identity_value(
        record.external_id
    ):
        evidence.append(MatchEvidence(kind="external_id", contribution=1, detail="provider id exact"))
        return ScoredCandidate(
            record=record, score=1, decision=MatchDecision.ACCEPT, evidence=tuple(evidence)
        )

    if (
        hints.source_url
        and record.source_url
        and hints.source_url.rstrip("/") == record.source_url.rstrip("/")
    ):
        evidence.append(MatchEvidence(kind="source_url", contribution=1, detail="source URL exact"))
        return ScoredCandidate(
            record=record, score=1, decision=MatchDecision.ACCEPT, evidence=tuple(evidence)
        )

    score = 0.0
    if (
        hints.code
        and record.code
        and normalize_identity_value(hints.code) == normalize_identity_value(record.code)
    ):
        contribution = 0.92
        score += contribution
        evidence.append(MatchEvidence(kind="code", contribution=contribution, detail="normalized code exact"))

    title_hint = hints.title or (hints.term if not hints.code else None)
    if title_hint:
        similarity = _title_similarity(title_hint, record.title)
        if similarity >= 0.35:
            contribution = similarity * 0.52
            score += contribution
            evidence.append(
                MatchEvidence(
                    kind="title",
                    contribution=round(contribution, 4),
                    detail=f"similarity={similarity:.3f}",
                )
            )

    if hints.duration_seconds is not None and record.runtime_seconds is not None:
        similarity = _duration_similarity(hints.duration_seconds, record.runtime_seconds)
        if similarity > 0:
            contribution = similarity * 0.16
            score += contribution
            evidence.append(
                MatchEvidence(
                    kind="duration",
                    contribution=round(contribution, 4),
                    detail=f"similarity={similarity:.3f}",
                )
            )

    score = min(1.0, round(score, 4))
    if score >= AUTO_ACCEPT_SCORE:
        decision = MatchDecision.ACCEPT
    elif score >= REVIEW_SCORE:
        decision = MatchDecision.REVIEW
    else:
        decision = MatchDecision.REJECT
    return ScoredCandidate(record=record, score=score, decision=decision, evidence=tuple(evidence))


def rank_candidates(hints: IdentityHints, records: list[ProviderRecord]) -> list[ScoredCandidate]:
    scored = [score_candidate(hints, record) for record in records]
    return sorted(scored, key=lambda item: (-item.score, item.record.provider, item.record.external_id))
