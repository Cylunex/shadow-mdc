import uuid
from datetime import UTC, date, datetime

from sqlalchemy import (
    JSON,
    Date,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from ..enums import (
    AssetState,
    CandidateState,
    CollectionKind,
    ContentFamily,
    IdentityKind,
    MediaCategory,
    RecognitionScope,
)


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
    recognition_scope: Mapped[str] = mapped_column(
        String(30),
        default=RecognitionScope.ALL.value,
        index=True,
    )
    organize_template: Mapped[str] = mapped_column(
        Text,
        default="{group}/{subgroup}/{actor}/{folder_name}/{media_name}.{ext}",
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class Work(Base):
    __tablename__ = "works"
    __table_args__ = (
        Index("ix_works_created_at", "created_at"),
        Index("ix_works_mobile_created_at_id", "created_at", "id"),
    )

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
    original_plot: Mapped[str | None] = mapped_column(Text)
    actors: Mapped[list[str]] = mapped_column(JSON, default=list)
    directors: Mapped[list[str]] = mapped_column(JSON, default=list)
    tags: Mapped[list[str]] = mapped_column(JSON, default=list)
    artwork: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    field_sources: Mapped[dict[str, str]] = mapped_column(JSON, default=dict)
    field_locks: Mapped[list[str]] = mapped_column(JSON, default=list)
    rating_value: Mapped[float | None] = mapped_column(Float)
    rating_max: Mapped[float | None] = mapped_column(Float)
    rating_count: Mapped[int | None] = mapped_column(Integer)
    rating_source: Mapped[str | None] = mapped_column(String(100))
    reviews: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WorkMagnet(Base):
    """Persisted magnet URI for copy/later use — never downloaded by this app."""

    __tablename__ = "work_magnets"
    __table_args__ = (
        UniqueConstraint("work_id", "info_hash", name="uq_work_magnet_hash"),
        Index("ix_work_magnet_work", "work_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    provider: Mapped[str] = mapped_column(String(100))
    info_hash: Mapped[str] = mapped_column(String(64))
    uri: Mapped[str] = mapped_column(Text)
    name: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(Integer)
    has_subtitle: Mapped[bool] = mapped_column(default=False)
    hd: Mapped[bool] = mapped_column(default=False)
    files_count: Mapped[int | None] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)



class Actor(Base):
    __tablename__ = "actors"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), unique=True, index=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    image_url: Mapped[str | None] = mapped_column(Text)
    x_handle: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WorkActor(Base):
    __tablename__ = "work_actors"
    __table_args__ = (
        UniqueConstraint("work_id", "actor_id", name="uq_work_actor"),
        Index("ix_work_actor_actor", "actor_id", "position"),
    )

    work_id: Mapped[str] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"),
        primary_key=True,
    )
    actor_id: Mapped[str] = mapped_column(
        ForeignKey("actors.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, default=0)


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
    modified_ns: Mapped[int] = mapped_column(Integer, default=0)
    duration_seconds: Mapped[float | None] = mapped_column(Float)
    media_info: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
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


class MatchFeedback(Base):
    __tablename__ = "match_feedback"

    asset_id: Mapped[str] = mapped_column(ForeignKey("media_assets.id", ondelete="CASCADE"), primary_key=True)
    hints: Mapped[dict[str, object]] = mapped_column(JSON)
    candidates: Mapped[list[dict[str, object]]] = mapped_column(JSON)
    chosen_provider: Mapped[str] = mapped_column(String(100))
    chosen_external_id: Mapped[str] = mapped_column(Text)
    chosen_code: Mapped[str | None] = mapped_column(String(100))


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


class TaskRun(Base):
    __tablename__ = "task_runs"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    scope: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), index=True, default="running")
    summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

class JobCheckpoint(Base):
    """Resumable cursor for long-running background jobs (one row per job key)."""

    __tablename__ = "job_checkpoints"

    key: Mapped[str] = mapped_column(String(300), primary_key=True)
    kind: Mapped[str] = mapped_column(String(50), index=True)
    status: Mapped[str] = mapped_column(String(30), index=True, default="running")
    cursor: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    stats: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    task_id: Mapped[str | None] = mapped_column(String(36))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LibraryScanSchedule(Base):
    __tablename__ = "library_scan_schedules"

    library_id: Mapped[str] = mapped_column(ForeignKey("libraries.id", ondelete="CASCADE"), primary_key=True)
    interval_hours: Mapped[int] = mapped_column(Integer)
    enabled_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class LibraryScanItem(Base):
    __tablename__ = "library_scan_items"

    task_id: Mapped[str] = mapped_column(ForeignKey("task_runs.id"), primary_key=True)
    path_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    obsolete_codes: Mapped[list[str]] = mapped_column(JSON, default=list)


class OrganizeJournalRecord(Base):
    """Reverse-operation log of one organize run, used for one-click undo."""

    __tablename__ = "organize_journals"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    task_id: Mapped[str] = mapped_column(String(36), index=True)
    library_id: Mapped[str | None] = mapped_column(String(36))
    mode: Mapped[str] = mapped_column(String(30))
    entries: Mapped[list[dict[str, object]]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    undo_summary: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)



class Collection(Base):
    """Collection entity (series/studio/platform/label) many-to-many with Work."""

    __tablename__ = "collections"
    __table_args__ = (
        UniqueConstraint("kind", "normalized_name", name="uq_collection_kind_name"),
        Index("ix_collection_kind", "kind"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300), index=True)
    kind: Mapped[str] = mapped_column(String(30), default=CollectionKind.SERIES.value)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class WorkCollection(Base):
    __tablename__ = "work_collections"
    __table_args__ = (
        UniqueConstraint("work_id", "collection_id", name="uq_work_collection"),
        Index("ix_work_collection_collection", "collection_id"),
    )

    work_id: Mapped[str] = mapped_column(
        ForeignKey("works.id", ondelete="CASCADE"),
        primary_key=True,
    )
    collection_id: Mapped[str] = mapped_column(
        ForeignKey("collections.id", ondelete="CASCADE"),
        primary_key=True,
    )


class PanOfflineTask(Base):
    """Local tracking for 115 cloud offline downloads (separate from work_magnets)."""

    __tablename__ = "pan_offline_tasks"
    __table_args__ = (
        Index("ix_pan_offline_work", "work_id"),
        Index("ix_pan_offline_status", "status"),
        Index("ix_pan_offline_hash", "info_hash"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"))
    magnet_id: Mapped[str | None] = mapped_column(String(36))
    info_hash: Mapped[str] = mapped_column(String(64))
    url: Mapped[str | None] = mapped_column(Text)
    directory_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30), default="running")  # running|done|failed|cancelled
    progress: Mapped[float] = mapped_column(Float, default=0.0)
    file_id: Mapped[str | None] = mapped_column(String(64))
    remote_name: Mapped[str | None] = mapped_column(Text)
    remote_path: Mapped[str | None] = mapped_column(Text)
    strm_path: Mapped[str | None] = mapped_column(Text)
    error: Mapped[str | None] = mapped_column(Text)
    # NULL / "115_open" = 115 Open Platform task; "openlist" = OpenList-managed task
    # (directory_id then holds the OpenList target path).
    backend: Mapped[str | None] = mapped_column(String(20))
    # OpenList task id (tid) for /api/task/offline_download polling.
    remote_task_id: Mapped[str | None] = mapped_column(String(64))
    # Durable magnet-failover checkpoint (attempts / current_hash / stalled / exhausted).
    recovery_json: Mapped[dict | None] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now, onupdate=utc_now)


class MediaEmbyBinding(Base):
    """Verified playback identity, independent of titles and temporary URLs."""

    __tablename__ = "media_emby_bindings"
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    server_id: Mapped[str] = mapped_column(String(100))
    item_id: Mapped[str] = mapped_column(String(100))


class WorkTaxonomy(Base):
    """Explicit release year; classification assignments live in WorkClassification."""

    __tablename__ = "work_taxonomy"
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    release_year: Mapped[int | None] = mapped_column(Integer)


class CatalogClassificationTerm(Base):
    __tablename__ = "catalog_classification_terms"
    id: Mapped[str] = mapped_column(String(100), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(40), index=True)
    name: Mapped[str] = mapped_column(String(300))
    normalized_name: Mapped[str] = mapped_column(String(300))
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    tag_id: Mapped[str | None] = mapped_column(String(4000))
    __table_args__ = (UniqueConstraint("dimension", "normalized_name"),)


class WorkClassification(Base):
    __tablename__ = "work_classifications"
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id", ondelete="CASCADE"), primary_key=True)
    term_id: Mapped[str] = mapped_column(
        ForeignKey("catalog_classification_terms.id", ondelete="RESTRICT"), primary_key=True,
    )
    __table_args__ = (Index("ix_work_classifications_term_work", "term_id", "work_id"),)


class CatalogPersonRecord(Base):
    __tablename__ = "catalog_people"
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    edits: Mapped[dict[str, object]] = mapped_column(JSON, default=dict)
    merged_into: Mapped[str | None] = mapped_column(String(36), index=True)
    revision: Mapped[int] = mapped_column(Integer, default=1)


class CatalogPersonReference(Base):
    __tablename__ = "catalog_person_references"
    ref: Mapped[str] = mapped_column(String(500), primary_key=True)
    person_id: Mapped[str] = mapped_column(ForeignKey("catalog_people.id"), index=True)


class CatalogPersonMerge(Base):
    __tablename__ = "catalog_person_merges"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    target_id: Mapped[str] = mapped_column(String(36))
    members: Mapped[list[str]] = mapped_column(JSON)
    revisions: Mapped[dict[str, int]] = mapped_column(JSON)
    previous_revisions: Mapped[dict[str, int]] = mapped_column(JSON, default=dict)
    undone: Mapped[bool] = mapped_column(default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CatalogOrganization(Base):
    __tablename__ = "catalog_organizations"
    id: Mapped[str] = mapped_column(ForeignKey("collections.id"), primary_key=True)
    aliases: Mapped[list[str]] = mapped_column(JSON, default=list)
    description: Mapped[str | None] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(String(36))
    revision: Mapped[int] = mapped_column(Integer, default=1)


class CatalogWorkOrganization(Base):
    __tablename__ = "catalog_work_organizations"
    __table_args__ = (Index("ix_catalog_org_work", "organization_id", "work_id"),)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), primary_key=True)
    organization_id: Mapped[str] = mapped_column(ForeignKey("collections.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), primary_key=True)


class CatalogPlaylist(Base):
    __tablename__ = "catalog_playlists"
    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=new_id)
    owner: Mapped[str] = mapped_column(String(100), index=True)
    name: Mapped[str] = mapped_column(String(300))
    work_ids: Mapped[list[str]] = mapped_column(JSON, default=list)
    revision: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utc_now)


class CatalogPlaylistRule(Base):
    __tablename__ = "catalog_playlist_rules"
    playlist_id: Mapped[str] = mapped_column(ForeignKey("catalog_playlists.id"), primary_key=True)
    filters: Mapped[dict[str, object]] = mapped_column(JSON)


class CatalogPersonCredit(Base):
    __tablename__ = "catalog_person_credits"
    person_id: Mapped[str] = mapped_column(ForeignKey("catalog_people.id"), primary_key=True)
    work_id: Mapped[str] = mapped_column(ForeignKey("works.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(30), primary_key=True, default="actor")
