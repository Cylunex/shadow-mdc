import uuid
from datetime import UTC, date, datetime

from sqlalchemy import JSON, Date, DateTime, Float, ForeignKey, Index, Integer, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..enums import AssetState, CandidateState, ContentFamily, IdentityKind, MediaCategory


def new_id() -> str:
    return str(uuid.uuid4())


def utc_now() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    pass


class Library(Base):
    __tablename__ = "libraries"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200), unique=True)
    root_path: Mapped[str] = mapped_column(Text, unique=True)
    category: Mapped[str] = mapped_column(String(30), default=MediaCategory.OTHER.value, index=True)
    recursive: Mapped[bool] = mapped_column(default=True)
    organize_template: Mapped[str] = mapped_column(
        Text,
        default="{studio}/{code_or_title}/{code_or_title}.{ext}",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Work(Base):
    __tablename__ = "works"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    title: Mapped[str] = mapped_column(Text)
    original_title: Mapped[str | None] = mapped_column(Text)
    primary_code: Mapped[str | None] = mapped_column(String(100), index=True)
    family: Mapped[str] = mapped_column(String(30), default=ContentFamily.UNKNOWN.value, index=True)
    category: Mapped[str] = mapped_column(String(30), default=MediaCategory.OTHER.value, index=True)
    release_date: Mapped[date | None] = mapped_column(Date)
    runtime_seconds: Mapped[int | None] = mapped_column(Integer)
    studio: Mapped[str | None] = mapped_column(String(300), index=True)
    label: Mapped[str | None] = mapped_column(String(300))
    series: Mapped[str | None] = mapped_column(String(300))
    plot: Mapped[str | None] = mapped_column(Text)
    actors: Mapped[list[str]] = mapped_column(JSON, default=list)
    directors: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    artwork: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class ExternalIdentity(Base):
    __tablename__ = "external_identities"
    __table_args__ = (
        UniqueConstraint("provider", "kind", "normalized_value", name="uq_identity_provider_kind_value"),
        Index("ix_identity_work", "work_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30), default=IdentityKind.PROVIDER_ID.value)
    value: Mapped[str] = mapped_column(Text)
    normalized_value: Mapped[str] = mapped_column(Text)
    source_url: Mapped[str | None] = mapped_column(Text)


class MediaAsset(Base):
    __tablename__ = "media_assets"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    library_id: Mapped[str] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"), index=True)
    work_id: Mapped[str | None] = mapped_column(ForeignKey("works.id", ondelete="SET NULL"), index=True)
    path: Mapped[str] = mapped_column(Text, unique=True)
    size: Mapped[int] = mapped_column(Integer)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    oshash: Mapped[str | None] = mapped_column(String(16), index=True)
    state: Mapped[str] = mapped_column(String(30), default=AssetState.NEW.value, index=True)
    hints: Mapped[dict[str, object]] = mapped_column(JSON)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class MatchCandidateRow(Base):
    __tablename__ = "match_candidates"
    __table_args__ = (
        UniqueConstraint("asset_id", "provider", "external_id", name="uq_candidate_asset_provider_external"),
        Index("ix_candidate_asset_state", "asset_id", "state"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(Text)
    score: Mapped[float] = mapped_column(Float)
    decision: Mapped[str] = mapped_column(String(30))
    state: Mapped[str] = mapped_column(String(30), default=CandidateState.PENDING.value)
    record: Mapped[dict[str, object]] = mapped_column(JSON)
    evidence: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class SourceSnapshot(Base):
    __tablename__ = "source_snapshots"
    __table_args__ = (
        UniqueConstraint("work_id", "provider", "external_id", name="uq_snapshot_work_provider_external"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), index=True)
    provider: Mapped[str] = mapped_column(String(100))
    external_id: Mapped[str] = mapped_column(Text)
    payload: Mapped[dict[str, object]] = mapped_column(JSON)
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
