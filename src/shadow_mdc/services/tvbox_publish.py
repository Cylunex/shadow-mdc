"""MDC-owned groups and revocable TVBox-compatible subscriptions."""

import hashlib
import json
import logging
import re
import secrets
from datetime import UTC, datetime, timedelta
from threading import Lock

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db.models import TvBoxBinding, TvBoxGroup, TvBoxPublication, TvBoxSite, utc_now
from ..db.repository import Database
from .tvbox import identity
from .tvbox_http import TvBoxError, http_url


class TvBoxFeedLogFilter(logging.Filter):
    _token = re.compile(r"(/api/tvbox-feed/)[^/?\s]+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(
                self._token.sub(r"\1[redacted]", part) if isinstance(part, str) else part
                for part in record.args
            )
        return True


class GroupInput(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    site_ids: list[str] = Field(default_factory=list, max_length=300)


class GroupView(GroupInput):
    model_config = ConfigDict(from_attributes=True)
    id: str
    site_aliases: dict[str, str] = Field(default_factory=dict)


class BindingInput(BaseModel):
    name: str = Field(min_length=1, max_length=120, pattern=r"\S")
    group_id: str | None = None
    days: int | None = Field(default=None, ge=1, le=365)


class BindingView(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: str
    name: str
    group_id: str | None
    revoked: bool
    needs_rotation: bool = False
    expires_at: datetime | None


class IssuedBinding(BindingView):
    path: str


class PublishedSite(BaseModel):
    key: str
    name: str
    type: int
    api: str


class PublicationGroup(BaseModel):
    id: str
    name: str
    sites: list[PublishedSite]


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class TvBoxPublishing:
    def __init__(self, database: Database) -> None:
        self.database = database
        self._publication_lock = Lock()

    def groups(self) -> list[GroupView]:
        with self.database.session() as session:
            return [GroupView.model_validate(row) for row in session.scalars(select(TvBoxGroup))]

    def save_group(self, data: GroupInput, group_id: str | None = None) -> GroupView:
        with self.database.session() as session:
            existing = session.scalar(select(TvBoxGroup).where(TvBoxGroup.name == data.name.strip()))
            if existing and existing.id != group_id:
                raise TvBoxError("此仓库名称已存在")
            row = session.get(TvBoxGroup, group_id) if group_id else TvBoxGroup()
            if row is None:
                raise LookupError("仓库不存在")
            ids = list(dict.fromkeys(data.site_ids))
            if ids and set(session.scalars(select(TvBoxSite.id).where(TvBoxSite.id.in_(ids)))) != set(ids):
                raise TvBoxError("部分站点不存在")
            row.name, row.site_ids = data.name.strip(), ids
            row.site_aliases = {key: value for key, value in (row.site_aliases or {}).items() if key in ids}
            session.add(row)
            session.flush()
            return GroupView.model_validate(row)

    def bindings(self) -> list[BindingView]:
        with self.database.session() as session:
            return [BindingView.model_validate(row) for row in session.scalars(select(TvBoxBinding))]

    def issue(self, data: BindingInput) -> IssuedBinding:
        with self.database.session() as session:
            if data.group_id and session.get(TvBoxGroup, data.group_id) is None:
                raise LookupError("仓库不存在")
            row = TvBoxBinding(
                name=data.name.strip(),
                group_id=data.group_id or None,
                revoked=False,
                expires_at=utc_now() + timedelta(days=data.days) if data.days else None,
            )
            return self._issue(session, row)

    def rotate(self, binding_id: str) -> IssuedBinding:
        with self.database.session() as session:
            row = session.get(TvBoxBinding, binding_id)
            if row is None:
                raise LookupError("订阅不存在")
            if row.revoked or self._expired(row):
                raise TvBoxError("请新建订阅以恢复已停用或过期的授权")
            return self._issue(session, row)

    def revoke(self, binding_id: str) -> BindingView:
        with self.database.session() as session:
            row = session.get(TvBoxBinding, binding_id)
            if row is None:
                raise LookupError("订阅不存在")
            row.revoked = True
            return BindingView.model_validate(row)

    @staticmethod
    def _expired(row: TvBoxBinding) -> bool:
        return bool(row.expires_at and row.expires_at.replace(tzinfo=UTC) <= utc_now())

    @staticmethod
    def _issue(session: Session, row: TvBoxBinding) -> IssuedBinding:
        token = secrets.token_urlsafe(32)
        row.token_hash = _token_hash(token)
        row.needs_rotation = False
        session.add(row)
        session.flush()
        return IssuedBinding(
            **BindingView.model_validate(row).model_dump(),
            path=f"/api/tvbox-feed/{token}/store.json",
        )

    @staticmethod
    def _current(session: Session, binding: TvBoxBinding) -> list[PublicationGroup]:
        sites: dict[str, PublishedSite] = {}
        for row in session.scalars(
            select(TvBoxSite)
            .where(TvBoxSite.enabled, ~TvBoxSite.hidden, TvBoxSite.active_revision.is_not(None))
            .order_by(TvBoxSite.priority.desc(), TvBoxSite.name, TvBoxSite.id)
        ):
            url = http_url(row.endpoint, allow_http=row.allow_http)
            # An endpoint with private query credentials is eligible for MDC browsing only.
            if any(
                key.lower() not in {"ac", "wd", "type", "t", "pg", "ids", "h", "at", "limit"}
                for key in url.params
            ):
                continue
            sites[row.id] = PublishedSite(key=row.id, name=row.name, type=row.protocol_type, api=row.endpoint)
        groups = [
            PublicationGroup(
                id=row.id,
                name=row.name,
                sites=[
                    sites[key].model_copy(update={"name": row.site_aliases[key]})
                    if key in row.site_aliases
                    else sites[key]
                    for key in row.site_ids
                    if key in sites
                ],
            )
            for row in session.scalars(select(TvBoxGroup).order_by(TvBoxGroup.name, TvBoxGroup.id))
            if not binding.group_id or row.id == binding.group_id
        ]
        if not binding.group_id:
            groups.insert(0, PublicationGroup(id="all", name="全部站点", sites=list(sites.values())))
        return groups

    def publication(self, token: str, revision: str | None = None) -> tuple[str, list[PublicationGroup]]:
        if not 24 <= len(token) <= 150:
            raise LookupError("订阅不存在")
        with self._publication_lock, self.database.session() as session:
            binding = session.scalar(
                select(TvBoxBinding).where(TvBoxBinding.token_hash == _token_hash(token))
            )
            if binding is None or binding.revoked or binding.needs_rotation or self._expired(binding):
                raise LookupError("订阅不存在")
            current = self._current(session, binding)
            if revision:
                saved = session.get(TvBoxPublication, (binding.id, revision))
                if saved is None:
                    raise LookupError("订阅版本不存在")
                snapshot = [PublicationGroup.model_validate(row) for row in saved.groups]
                active = {group.id: {site.key for site in group.sites} for group in current}
                if any(
                    group.id not in active or any(site.key not in active[group.id] for site in group.sites)
                    for group in snapshot
                ):
                    raise LookupError("订阅版本已撤回")
                return revision, snapshot

            data = [row.model_dump() for row in current]
            version = identity(json.dumps(data, ensure_ascii=False, sort_keys=True))
            if session.get(TvBoxPublication, (binding.id, version)) is None:
                session.add(TvBoxPublication(binding_id=binding.id, revision=version, groups=data))
                older = list(
                    session.scalars(
                        select(TvBoxPublication)
                        .where(TvBoxPublication.binding_id == binding.id)
                        .order_by(TvBoxPublication.created_at.desc(), TvBoxPublication.revision)
                        .offset(19)
                    )
                )
                for row in older:
                    session.delete(row)
            return version, current
