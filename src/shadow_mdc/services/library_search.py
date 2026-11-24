"""Local library search ranking for codes, titles, actors, and tags."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

from ..normalize_code import normalize_code, to_comparison_key

_WS = re.compile(r"\s+")
_CODEISH = re.compile(r"^[A-Za-z]{1,10}[-_\s]?\d{2,7}[A-Za-z]?$", re.ASCII)


@dataclass(frozen=True)
class RankedHit:
    """Scored match for a library work card."""

    work_id: str
    score: float
    reason: str


@dataclass(frozen=True)
class _PreparedQuery:
    needle: str
    q_code: str
    q_key: str
    is_codeish: bool


def normalize_query(raw: str) -> str:
    return _WS.sub(" ", unicodedata.normalize("NFKC", raw or "").strip()).lower()


def _prepare_query(query: str) -> _PreparedQuery | None:
    needle = normalize_query(query)
    if not needle:
        return None
    return _PreparedQuery(
        needle=needle,
        q_code=normalize_code(query).lower(),
        q_key=to_comparison_key(query).lower(),
        is_codeish=bool(_CODEISH.match(query.strip())),
    )


def _score_prepared_work_fields(
    prepared: _PreparedQuery,
    *,
    work_id: str,
    title: str,
    original_title: str | None,
    primary_code: str | None,
    actors: list[str] | tuple[str, ...] | None,
    studio: str | None,
    series: str | None,
    tags: list[str] | tuple[str, ...] | None,
) -> RankedHit | None:
    needle = prepared.needle
    q_code = prepared.q_code
    q_key = prepared.q_key

    code = (primary_code or "").strip()
    code_norm = normalize_code(code).lower() if code else ""
    code_key = to_comparison_key(code).lower() if code else ""

    best = 0.0
    reason = ""

    if code_norm and q_code and (code_norm == q_code or (code_key and code_key == q_key)):
        best, reason = 100.0, "code_exact"
    elif code_norm and q_code and code_norm.startswith(q_code):
        best, reason = 92.0, "code_prefix"
    elif code_key and q_key and q_key in code_key:
        best, reason = 84.0, "code_contains"
    elif prepared.is_codeish and code_key and q_key and code_key.startswith(q_key):
        best, reason = 88.0, "code_prefix"

    title_l = (title or "").lower()
    original_l = (original_title or "").lower()
    if needle == title_l or (original_l and needle == original_l):
        if best < 75:
            best, reason = 75.0, "title_exact"
    elif title_l.startswith(needle) or (original_l and original_l.startswith(needle)):
        if best < 65:
            best, reason = 65.0, "title_prefix"
    elif best < 48 and (needle in title_l or (original_l and needle in original_l)):
        best, reason = 48.0, "title_contains"

    for actor in actors or ():
        name = str(actor).strip().lower()
        if not name:
            continue
        if name == needle:
            if best < 70:
                best, reason = 70.0, "actor_exact"
            break
        if best < 52 and (needle in name or (len(name) >= 2 and name in needle)):
            best, reason = 52.0, "actor_contains"

    studio_l = (studio or "").lower()
    if studio_l and needle in studio_l and best < 36:
        best, reason = 36.0, "studio"

    series_l = (series or "").lower()
    if series_l and needle in series_l and best < 34:
        best, reason = 34.0, "series"

    for tag in tags or ():
        tag_l = str(tag).strip().lower()
        if tag_l and (needle == tag_l or needle in tag_l):
            if best < 30:
                best, reason = 30.0, "tag"
            break

    if best <= 0:
        return None
    return RankedHit(work_id=work_id, score=best, reason=reason)


def score_work_fields(
    query: str,
    *,
    work_id: str,
    title: str,
    original_title: str | None,
    primary_code: str | None,
    actors: list[str] | tuple[str, ...] | None,
    studio: str | None,
    series: str | None,
    tags: list[str] | tuple[str, ...] | None,
) -> RankedHit | None:
    """Return a ranked hit when ``query`` matches any indexed field.

    Ranking (higher is better):
    - exact / comparison-key code match
    - code prefix
    - exact actor name
    - title prefix / contains
    - actor / studio / series / tag contains
    """

    prepared = _prepare_query(query)
    if prepared is None:
        return None
    return _score_prepared_work_fields(
        prepared,
        work_id=work_id,
        title=title,
        original_title=original_title,
        primary_code=primary_code,
        actors=actors,
        studio=studio,
        series=series,
        tags=tags,
    )


def rank_work_dicts(
    query: str,
    rows: list[dict[str, object]],
    *,
    limit: int = 48,
    offset: int = 0,
) -> tuple[list[dict[str, object]], int]:
    """Rank already-serialized work card dicts; return page + total hits."""

    prepared = _prepare_query(query)
    if prepared is None:
        return [], 0

    scored: list[tuple[float, int, dict[str, object]]] = []
    for index, row in enumerate(rows):
        raw_actors = row.get("actors")
        actors = raw_actors if isinstance(raw_actors, (list, tuple)) else ()
        raw_tags = row.get("display_tags") or row.get("tags")
        tags = raw_tags if isinstance(raw_tags, (list, tuple)) else ()
        hit = _score_prepared_work_fields(
            prepared,
            work_id=str(row.get("id") or ""),
            title=str(row.get("title") or ""),
            original_title=str(row["original_title"]) if row.get("original_title") else None,
            primary_code=str(row["primary_code"]) if row.get("primary_code") else None,
            actors=actors,
            studio=str(row["studio"]) if row.get("studio") else None,
            series=str(row["series"]) if row.get("series") else None,
            tags=tags,
        )
        if hit is None:
            continue
        scored.append((hit.score, index, row))
    scored.sort(key=lambda item: (-item[0], item[1]))
    total = len(scored)
    page = [item[2] for item in scored[offset : offset + limit]]
    return page, total
