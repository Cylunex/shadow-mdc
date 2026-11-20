"""Dirty-key journals and on-disk projections for the catalog people routes."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Iterable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from ..db.models import Work
from .catalog_people import people as build_profiles


class PersonCard(BaseModel):
    id: str
    name: str
    aliases: list[str]
    image_url: str | None
    work_count: int
    revision: int
    same_name: bool


def fold_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def file_signature(paths: Iterable[Path]) -> str:
    signatures: list[tuple[int, int, int, int, int] | None] = []
    for path in paths:
        try:
            value = path.stat()
            signatures.append((value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns, value.st_ctime_ns))
        except FileNotFoundError:
            signatures.append(None)
    return json.dumps(signatures)


def state_value(session: Session, key: str) -> str | None:
    return session.execute(text("SELECT value FROM mdc_people_state WHERE key=:key"), {"key": key}).scalar_one_or_none()


def set_state(session: Session, key: str, value: str) -> None:
    session.execute(
        text("INSERT OR REPLACE INTO mdc_people_state VALUES (:key,:value)"), {"key": key, "value": value},
    )


def take_dirty(session: Session, kind: str) -> set[str]:
    query = {"kind": kind}
    if session.execute(text("SELECT 1 FROM mdc_people_dirty WHERE kind=:kind LIMIT 1"), query).first() is None:
        return set()
    return set(session.execute(text("DELETE FROM mdc_people_dirty WHERE kind=:kind RETURNING id"), query).scalars())


def update_work_names(session: Session, work_ids: set[str] | None) -> set[str]:
    if work_ids is None:
        session.execute(text("DELETE FROM mdc_people_work_names"))
    elif not work_ids:
        return set()
    old_names: set[str] = set()
    if work_ids is not None:
        old_names.update(session.execute(text("""
            DELETE FROM mdc_people_work_names WHERE work_id IN (SELECT value FROM json_each(:keys)) RETURNING name
        """), {"keys": json.dumps(sorted(work_ids))}).scalars())
    source = select(Work.id, Work.actors)
    if work_ids is not None:
        source = source.where(Work.id.in_(work_ids))
    rows = [
        {"work_id": key, "name": name}
        for key, casts in session.execute(source)
        for name in {fold_name(old) for old in (casts or []) if old.strip()}
    ]
    if rows:
        session.execute(text("INSERT INTO mdc_people_work_names VALUES (:work_id,:name)"), rows)
    return old_names | {row["name"] for row in rows}


def work_ids_for_names(session: Session, names: set[str]) -> set[str]:
    if not names:
        return set()
    return set(session.execute(text("""
        SELECT work_id FROM mdc_people_work_names WHERE name IN (SELECT value FROM json_each(:names))
    """), {"names": json.dumps(sorted(names))}).scalars())


def refresh_cards(session: Session) -> int:
    dirty = take_dirty(session, "card")
    states = dict(session.execute(text(
        "SELECT key, value FROM mdc_people_state WHERE key IN ('cards', 'portrait_backfill')"
    )).tuples().all())
    version = states.get("cards")
    backfill = False
    if "portrait_backfill" in states:
        backfill = session.execute(text(
            "DELETE FROM mdc_people_state WHERE key='portrait_backfill' RETURNING key"
        )).scalar_one_or_none() is not None
        version = state_value(session, "cards")
    if version is not None and not dirty and not backfill:
        return int(version)
    if version is None or backfill:
        session.execute(text("DELETE FROM mdc_people_cards"))
        session.execute(text("DELETE FROM mdc_people_card_works"))
        session.execute(text("DELETE FROM mdc_people_excluded_names"))
        profiles = build_profiles(session)
    else:
        roots = session.execute(text("""
            WITH RECURSIVE redirects(id, parent) AS (
                SELECT id, merged_into FROM catalog_people WHERE id IN (
                    SELECT value FROM json_each(:dirty)
                )
                UNION
                SELECT p.id,p.merged_into FROM catalog_people p JOIN redirects r ON p.id=r.parent
            )
            SELECT id FROM redirects WHERE parent IS NULL
        """), {"dirty": json.dumps(sorted(dirty))}).scalars()
        affected = dirty | set(roots)
        session.execute(text("DELETE FROM mdc_people_cards WHERE id IN (SELECT value FROM json_each(:keys))"),
                        {"keys": json.dumps(sorted(affected))})
        session.execute(text("DELETE FROM mdc_people_card_works WHERE person_id IN (SELECT value FROM json_each(:keys))"),
                        {"keys": json.dumps(sorted(affected))})
        session.execute(text("""
            DELETE FROM mdc_people_excluded_names WHERE person_id IN (SELECT value FROM json_each(:keys))
        """), {"keys": json.dumps(sorted(affected))})
        profiles = build_profiles(session, root_ids=affected)
    rows = [
        {"id": doc.id, "name": doc.name, "sort": fold_name(doc.name), "aliases": json.dumps(doc.aliases),
         "search": "\n".join(fold_name(name) for name in (doc.name, *doc.aliases)), "works": len(doc.work_ids),
         "revision": doc.revision, "portrait": hashlib.sha256((doc.image_url or "").encode()).hexdigest()[:16],
         "url": doc.image_url}
        for doc in profiles
    ]
    if rows:
        session.execute(text("""
            INSERT INTO mdc_people_cards
                (id, name, sort_name, aliases, search, works, revision, portrait_revision, portrait_url)
            VALUES (:id,:name,:sort,:aliases,:search,:works,:revision,:portrait,:url)
        """), rows)
    credits = [{"person": doc.id, "work": work} for doc in profiles for work in doc.work_ids]
    if credits:
        session.execute(text("INSERT INTO mdc_people_card_works VALUES (:person,:work)"), credits)
    excluded = [
        {"person": doc.id, "work": work_id, "name": name}
        for doc in profiles for work_id in doc.excluded_work_ids
        for name in {fold_name(value) for value in (doc.name, *doc.aliases)}
    ]
    if excluded:
        session.execute(text("INSERT INTO mdc_people_excluded_names VALUES (:person,:work,:name)"), excluded)
    result = int(version or "0") + 1
    set_state(session, "cards", str(result))
    return result


def page_cards(
    session: Session, query: str, sort: Literal["works", "name"], offset: int, limit: int,
) -> list[PersonCard]:
    order = "works DESC," if sort == "works" else ""
    rows = session.execute(text(f"""
        SELECT p.id,p.name,p.aliases,p.works,p.revision,p.portrait_revision,
            EXISTS(SELECT 1 FROM mdc_people_cards other WHERE other.name=p.name AND other.id<>p.id) AS same_name
        FROM mdc_people_cards p
        WHERE :query='' OR instr(p.search,:query)>0
        ORDER BY {order} p.sort_name,p.id LIMIT :limit OFFSET :offset
    """), {"query": query, "offset": offset, "limit": limit})
    return [
        PersonCard(id=key, name=name, aliases=json.loads(aliases), work_count=works,
                   revision=revision, same_name=bool(same_name),
                   image_url=f"/api/media/v1/catalog/people/{key}/image?v={portrait}")
        for key, name, aliases, works, revision, portrait, same_name in rows
    ]


def web_cards(
    session: Session, query: str, sort: Literal["works", "name"], offset: int, limit: int,
) -> tuple[list[PersonCard], int]:
    where = """
        WHERE :query='' OR instr(mdc_casefold(p.name), :query)>0 OR EXISTS (
            SELECT 1 FROM json_each(p.aliases) WHERE instr(mdc_casefold(value), :query)>0
        )
    """
    params = {"query": query, "offset": offset, "limit": limit}
    total = int(session.execute(text(
        f"SELECT count(*) FROM mdc_people_cards p {where}"
    ), params).scalar_one())
    if offset >= total:
        return [], total
    order = "p.works DESC," if sort == "works" else ""
    rows = session.execute(text(f"""
        SELECT p.id,p.name,p.aliases,p.works,p.revision,p.portrait_url, EXISTS (
            SELECT 1 FROM mdc_people_cards other WHERE other.name=p.name AND other.id<>p.id
        ) FROM mdc_people_cards p {where}
        ORDER BY {order} mdc_casefold(p.name), p.id LIMIT :limit OFFSET :offset
    """), params)
    return [
        PersonCard(id=key, name=name, aliases=json.loads(aliases)[:2], work_count=works,
                   revision=revision, same_name=bool(same_name), image_url=url)
        for key, name, aliases, works, revision, url, same_name in rows
    ], total
