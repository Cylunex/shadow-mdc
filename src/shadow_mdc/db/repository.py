import unicodedata
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, exists, select
from sqlalchemy.orm import Session, sessionmaker

from ..domain import IdentityHints, MatchEvidence, MediaTechnicalInfo, ProviderRecord, ScoredCandidate
from ..enums import (
    AssetState,
    CandidateState,
    IdentityKind,
    MatchDecision,
    MediaCategory,
    RecognitionScope,
)
from ..identity import normalize_identity_value
from .models import (
    Actor,
    Base,
    ExternalIdentity,
    Library,
    MatchCandidateRow,
    MediaAsset,
    SourceSnapshot,
    TaskRun,
    Work,
    WorkActor,
    utc_now,
)

_JAV_ACTOR_PROVIDER_PRIORITY = {
    "fanza": 0,
    "jav321": 1,
    "mgstage": 2,
    "javlibrary": 3,
    "r18dev": 4,
    "javdb": 5,
    "javbus": 6,
    "airav": 7,
    "avsox": 8,
    "freejavbt": 100,
}


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False, "timeout": 30.0} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            self._migrate_sqlite()

    def _migrate_sqlite(self) -> None:
        with self.engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA journal_mode=WAL")
            connection.exec_driver_sql("PRAGMA busy_timeout=30000")
            connection.commit()
        with self.engine.begin() as connection:
            library_columns = {
                str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(libraries)")
            }
            if "category" not in library_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE libraries ADD COLUMN category VARCHAR(30) NOT NULL DEFAULT 'Other'"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_libraries_category ON libraries (category)"
                )
            if "recognition_scope" not in library_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE libraries ADD COLUMN recognition_scope VARCHAR(30) NOT NULL DEFAULT 'all'"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_libraries_recognition_scope "
                    "ON libraries (recognition_scope)"
                )
            work_columns = {str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(works)")}
            if "category" not in work_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE works ADD COLUMN category VARCHAR(30) NOT NULL DEFAULT 'Other'"
                )
                connection.exec_driver_sql("CREATE INDEX IF NOT EXISTS ix_works_category ON works (category)")
            if "field_sources" not in work_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE works ADD COLUMN field_sources JSON NOT NULL DEFAULT '{}'"
                )
            asset_columns = {
                str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(media_assets)")
            }
            if "modified_ns" not in asset_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE media_assets ADD COLUMN modified_ns INTEGER NOT NULL DEFAULT 0"
                )
            if "media_info" not in asset_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE media_assets ADD COLUMN media_info JSON NOT NULL DEFAULT '{}'"
                )
            connection.exec_driver_sql(
                """
                UPDATE libraries
                SET category = CASE
                    WHEN name LIKE '%国产%' OR lower(name) LIKE '%china%' THEN 'China'
                    WHEN name LIKE '%韩国%' OR name LIKE '%韓國%' OR lower(name) LIKE '%korea%' THEN 'Korea'
                    WHEN name LIKE '%欧美%' OR lower(name) LIKE '%europe%' THEN 'Europe'
                    WHEN name LIKE '%日本%' OR lower(name) LIKE '%japan%' OR lower(name) = 'jav' THEN 'Japan'
                    ELSE category
                END
                WHERE category = 'Other'
                """
            )
            connection.exec_driver_sql(
                """
                UPDATE libraries
                SET organize_template = '{group}/{subgroup}/{actor}/{folder_name}/{media_name}.{ext}'
                WHERE organize_template IN (
                    '{studio}/{code_or_title}/{code_or_title}.{ext}',
                    '{category}/{family}/{actor}/{code_or_title}/{code_or_title}.{ext}'
                )
                """
            )
            connection.exec_driver_sql(
                """
                UPDATE works
                SET category = CASE family
                    WHEN 'jav' THEN 'Japan'
                    WHEN 'chinese' THEN 'China'
                    WHEN 'korean' THEN 'Korea'
                    WHEN 'western' THEN 'Europe'
                    ELSE category
                END
                WHERE category = 'Other'
                """
            )

    @contextmanager
    def session(self) -> Iterator[Session]:
        session = self._sessions()
        try:
            yield session
            session.commit()
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class Repository:
    def __init__(self, session: Session):
        self._session = session

    def create_library(
        self,
        *,
        name: str,
        root_path: str,
        category: MediaCategory = MediaCategory.OTHER,
        recursive: bool,
        organize_template: str,
        recognition_scope: RecognitionScope = RecognitionScope.ALL,
    ) -> Library:
        library = Library(
            name=name,
            root_path=str(Path(root_path).resolve()),
            category=category.value,
            recursive=recursive,
            recognition_scope=recognition_scope.value,
            organize_template=organize_template,
        )
        self._session.add(library)
        self._session.flush()
        return library

    def update_library_recognition_scope(
        self,
        library: Library,
        recognition_scope: RecognitionScope,
    ) -> Library:
        library.recognition_scope = recognition_scope.value
        self._session.flush()
        return library

    def list_libraries(self) -> list[Library]:
        return list(self._session.scalars(select(Library).order_by(Library.name)))

    def get_library(self, library_id: str) -> Library | None:
        return self._session.get(Library, library_id)

    def create_task_run(self, *, kind: str, scope: str) -> TaskRun:
        task = TaskRun(kind=kind, scope=scope)
        self._session.add(task)
        self._session.flush()
        return task

    def finish_task_run(
        self,
        task: TaskRun,
        *,
        status: str,
        summary: dict[str, object],
        error: str | None = None,
    ) -> None:
        task.status = status
        task.summary = summary
        task.error = error
        task.finished_at = utc_now()
        self._session.flush()

    def list_task_runs(self, *, limit: int = 100) -> list[TaskRun]:
        statement = select(TaskRun).order_by(TaskRun.created_at.desc()).limit(limit)
        return list(self._session.scalars(statement))

    def upsert_asset(
        self,
        *,
        library_id: str,
        path: str,
        size: int,
        modified_ns: int = 0,
        duration_seconds: float | None,
        oshash: str | None,
        hints: IdentityHints,
        media_info: MediaTechnicalInfo | None = None,
    ) -> tuple[MediaAsset, bool]:
        absolute = str(Path(path).resolve())
        existing = self._session.scalar(select(MediaAsset).where(MediaAsset.path == absolute))
        if existing is not None:
            existing.size = size
            existing.modified_ns = modified_ns
            existing.duration_seconds = duration_seconds
            existing.media_info = (media_info or MediaTechnicalInfo()).model_dump(mode="json")
            existing.oshash = oshash
            existing.hints = hints.model_dump(mode="json")
            if existing.state == AssetState.IGNORED.value:
                existing.state = AssetState.NEW.value
                existing.error = None
            return existing, False
        asset = MediaAsset(
            library_id=library_id,
            path=absolute,
            size=size,
            modified_ns=modified_ns,
            duration_seconds=duration_seconds,
            media_info=(media_info or MediaTechnicalInfo()).model_dump(mode="json"),
            oshash=oshash,
            hints=hints.model_dump(mode="json"),
        )
        self._session.add(asset)
        self._session.flush()
        return asset, True

    def get_asset(self, asset_id: str) -> MediaAsset | None:
        return self._session.get(MediaAsset, asset_id)

    def get_asset_by_path(self, path: str) -> MediaAsset | None:
        absolute = str(Path(path).resolve())
        return self._session.scalar(select(MediaAsset).where(MediaAsset.path == absolute))

    def list_assets(self, *, state: str | None = None) -> list[MediaAsset]:
        statement = select(MediaAsset)
        if state is not None:
            statement = statement.where(MediaAsset.state == state)
        else:
            statement = statement.where(MediaAsset.state != AssetState.IGNORED.value)
        return list(self._session.scalars(statement.order_by(MediaAsset.created_at.desc())))

    def list_library_assets(self, library_id: str, *, identified_only: bool = False) -> list[MediaAsset]:
        statement = select(MediaAsset).where(
            MediaAsset.library_id == library_id,
            MediaAsset.state != AssetState.IGNORED.value,
        )
        if identified_only:
            statement = statement.where(MediaAsset.work_id.is_not(None))
        return list(self._session.scalars(statement.order_by(MediaAsset.path)))

    def list_unverified_local_assets(self) -> list[MediaAsset]:
        remote_evidence = exists(
            select(SourceSnapshot.id).where(
                SourceSnapshot.work_id == Work.id,
                SourceSnapshot.provider != "local-path",
            )
        )
        accepted_evidence = exists(
            select(MatchCandidateRow.id)
            .join(MediaAsset, MatchCandidateRow.asset_id == MediaAsset.id)
            .where(
                MediaAsset.work_id == Work.id,
                MatchCandidateRow.provider != "local-path",
                MatchCandidateRow.state == CandidateState.ACCEPTED.value,
            )
        )
        statement = (
            select(MediaAsset)
            .join(Work, MediaAsset.work_id == Work.id)
            .where(~remote_evidence, ~accepted_evidence)
            .order_by(MediaAsset.path)
        )
        return list(self._session.scalars(statement))

    def ignore_asset_by_path(self, path: str, reason: str) -> bool:
        absolute = str(Path(path).resolve())
        asset = self._session.scalar(select(MediaAsset).where(MediaAsset.path == absolute))
        if asset is None:
            return False
        asset.state = AssetState.IGNORED.value
        asset.error = reason
        self._session.flush()
        return True

    def update_asset_path(self, asset: MediaAsset, path: str) -> None:
        asset.path = str(Path(path).resolve())
        self._session.flush()

    def update_asset_hints(self, asset: MediaAsset, hints: IdentityHints) -> None:
        asset.hints = hints.model_dump(mode="json")
        asset.error = None
        self._session.flush()

    def save_candidates(
        self, asset: MediaAsset, candidates: list[ScoredCandidate]
    ) -> list[MatchCandidateRow]:
        saved: list[MatchCandidateRow] = []
        for candidate in candidates:
            record = candidate.record
            row = self._session.scalar(
                select(MatchCandidateRow).where(
                    MatchCandidateRow.asset_id == asset.id,
                    MatchCandidateRow.provider == record.provider,
                    MatchCandidateRow.external_id == record.external_id,
                )
            )
            if row is None:
                row = MatchCandidateRow(
                    asset_id=asset.id,
                    provider=record.provider,
                    external_id=record.external_id,
                    score=candidate.score,
                    decision=candidate.decision.value,
                    record=record.model_dump(mode="json"),
                    evidence=[item.model_dump(mode="json") for item in candidate.evidence],
                )
                self._session.add(row)
            else:
                row.score = candidate.score
                row.decision = candidate.decision.value
                row.record = record.model_dump(mode="json")
                row.evidence = [item.model_dump(mode="json") for item in candidate.evidence]
            saved.append(row)
        if asset.work_id is not None:
            asset.state = AssetState.IDENTIFIED.value
            asset.error = None if saved else "no remote candidates"
        else:
            asset.state = AssetState.REVIEW.value if saved else AssetState.ERROR.value
            asset.error = None if saved else "no candidates"
        self._session.flush()
        return saved

    def list_candidates(self, asset_id: str) -> list[MatchCandidateRow]:
        statement = (
            select(MatchCandidateRow)
            .where(MatchCandidateRow.asset_id == asset_id)
            .order_by(MatchCandidateRow.score.desc())
        )
        return list(self._session.scalars(statement))

    def get_candidate(self, candidate_id: str) -> MatchCandidateRow | None:
        return self._session.get(MatchCandidateRow, candidate_id)

    def accept_candidate(self, candidate_id: str) -> Work:
        candidate = self.get_candidate(candidate_id)
        if candidate is None:
            raise LookupError(f"candidate {candidate_id} not found")
        asset = self.get_asset(candidate.asset_id)
        if asset is None:
            raise LookupError(f"asset {candidate.asset_id} not found")
        record = ProviderRecord.model_validate(candidate.record)
        work = self._resolve_existing_work(record)
        if work is None:
            work = self._create_work(record)
        else:
            self._merge_provider_record(work, record)
        self._add_record_identities(work, record)
        self._upsert_snapshot(work, record)
        asset.work_id = work.id
        asset.state = AssetState.IDENTIFIED.value
        asset.error = None
        candidate.state = CandidateState.ACCEPTED.value
        for other in self.list_candidates(asset.id):
            if other.id != candidate.id and other.state == CandidateState.PENDING.value:
                other.state = CandidateState.REJECTED.value
        self._session.flush()
        return work

    def merge_candidate_into_work(self, candidate_id: str, work_id: str) -> Work:
        candidate = self.get_candidate(candidate_id)
        work = self.get_work(work_id)
        if candidate is None:
            raise LookupError(f"candidate {candidate_id} not found")
        if work is None:
            raise LookupError(f"work {work_id} not found")
        record = ProviderRecord.model_validate(candidate.record)
        self._merge_provider_record(work, record, overwrite=False)
        self._add_record_identities(work, record)
        self._upsert_snapshot(work, record)
        candidate.state = CandidateState.ACCEPTED.value
        self._session.flush()
        return work

    def catalog_asset(self, asset: MediaAsset, record: ProviderRecord) -> Work:
        work = self.get_work(asset.work_id) if asset.work_id is not None else None
        if work is None:
            work = self._resolve_existing_work(record)
        if work is None:
            work = self._create_work(record)
        else:
            self._merge_local_record(work, record)
        self._add_record_identities(work, record)
        self._upsert_snapshot(work, record)
        asset.work_id = work.id
        asset.state = AssetState.IDENTIFIED.value
        asset.error = None
        self._session.flush()
        return work

    def refresh_local_catalog_asset(self, asset: MediaAsset, record: ProviderRecord) -> Work:
        """Replace path-derived fields after a user confirms a broader directory rule."""

        work = self.get_work(asset.work_id) if asset.work_id is not None else None
        if work is None:
            return self.catalog_asset(asset, record)
        self._replace_local_record(work, record)
        self._add_record_identities(work, record)
        self._upsert_snapshot(work, record)
        asset.state = AssetState.IDENTIFIED.value
        asset.error = None
        self._session.flush()
        return work

    def queue_local_candidate(self, asset: MediaAsset, record: ProviderRecord) -> bool:
        """Queue path-derived metadata unless the asset is already backed by verified evidence."""

        candidate = ScoredCandidate(
            record=record,
            score=0.6,
            decision=MatchDecision.REVIEW,
            evidence=(
                MatchEvidence(
                    kind="local_path",
                    contribution=0.6,
                    detail="filename and directory hints; not a verified metadata match",
                ),
            ),
        )
        work = self.get_work(asset.work_id) if asset.work_id is not None else None
        if work is not None:
            if self._work_has_verified_evidence(work.id):
                self._merge_local_record(work, record)
            else:
                self._replace_local_record(work, record)
            self._add_record_identities(work, record)
            self._upsert_snapshot(work, record)
            asset.state = AssetState.IDENTIFIED.value
            asset.error = None
            self.save_candidates(asset, [candidate])
            return False
        self.save_candidates(asset, [candidate])
        return True

    def accept_local_candidate(self, asset_id: str) -> Work | None:
        asset = self.get_asset(asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        if asset.work_id is not None:
            return self.get_work(asset.work_id)
        candidate = self._session.scalar(
            select(MatchCandidateRow)
            .where(
                MatchCandidateRow.asset_id == asset.id,
                MatchCandidateRow.provider == "local-path",
            )
            .order_by(MatchCandidateRow.created_at.desc())
            .limit(1)
        )
        if candidate is None:
            return None
        return self.accept_candidate(candidate.id)

    def local_candidate_record(self, asset_id: str) -> ProviderRecord | None:
        candidate = self._session.scalar(
            select(MatchCandidateRow)
            .where(
                MatchCandidateRow.asset_id == asset_id,
                MatchCandidateRow.provider == "local-path",
            )
            .order_by(MatchCandidateRow.created_at.desc())
            .limit(1)
        )
        return ProviderRecord.model_validate(candidate.record) if candidate is not None else None

    def upsert_provider_record(self, record: ProviderRecord, *, overwrite: bool) -> Work:
        work = self._resolve_existing_work(record)
        if work is None:
            work = self._create_work(record)
        else:
            self._merge_provider_record(work, record, overwrite=overwrite)
        self._add_record_identities(work, record)
        self._upsert_snapshot(work, record)
        self._session.flush()
        return work

    def list_works(self) -> list[Work]:
        return list(self._session.scalars(select(Work).order_by(Work.created_at.desc())))

    def sync_all_work_actors(self) -> None:
        for work in self.list_works():
            self._sync_work_actors(work)
        self._session.flush()

    def repair_jav_actor_sources(self) -> int:
        """Rebuild JAV actor lists from the most trustworthy stored source snapshot."""

        repaired = 0
        for work in self.list_works():
            snapshots = self._session.scalars(
                select(SourceSnapshot).where(SourceSnapshot.work_id == work.id)
            )
            records = [
                ProviderRecord.model_validate(snapshot.payload)
                for snapshot in snapshots
                if snapshot.provider in _JAV_ACTOR_PROVIDER_PRIORITY
            ]
            records = [record for record in records if record.actors]
            if not records:
                continue
            best = min(records, key=lambda record: _JAV_ACTOR_PROVIDER_PRIORITY[record.provider])
            actors = list(best.actors)
            sources = dict(work.field_sources or {})
            changed = work.actors != actors or sources.get("actors") != best.provider
            if not changed:
                continue
            work.actors = actors
            sources["actors"] = best.provider
            work.field_sources = sources
            self._sync_work_actors(work)
            repaired += 1
        self._session.flush()
        return repaired

    def list_actor_work_relations(self) -> list[tuple[Actor, Work]]:
        statement = (
            select(Actor, Work)
            .join(WorkActor, WorkActor.actor_id == Actor.id)
            .join(Work, Work.id == WorkActor.work_id)
            .order_by(Actor.name, WorkActor.position, Work.release_date, Work.title)
        )
        return [(actor, work) for actor, work in self._session.execute(statement)]

    def actors_for_work(self, work_id: str) -> list[Actor]:
        statement = (
            select(Actor)
            .join(WorkActor, WorkActor.actor_id == Actor.id)
            .where(WorkActor.work_id == work_id)
            .order_by(WorkActor.position)
        )
        return list(self._session.scalars(statement))

    def first_asset_for_work(self, work_id: str) -> MediaAsset | None:
        return self._session.scalar(
            select(MediaAsset).where(MediaAsset.work_id == work_id).order_by(MediaAsset.created_at).limit(1)
        )

    def get_work(self, work_id: str) -> Work | None:
        return self._session.get(Work, work_id)

    def find_work_by_code(self, code: str) -> Work | None:
        identity = self._session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == "global",
                ExternalIdentity.kind == IdentityKind.CODE.value,
                ExternalIdentity.normalized_value == normalize_identity_value(code),
            )
        )
        if identity is not None:
            return self.get_work(identity.work_id)
        return self._session.scalar(
            select(Work).where(Work.primary_code == code).order_by(Work.created_at).limit(1)
        )

    def attach_asset_to_work(self, asset: MediaAsset, work: Work) -> None:
        asset.work_id = work.id
        asset.state = AssetState.IDENTIFIED.value
        asset.error = None
        self._session.flush()

    def identities_for_work(self, work_id: str) -> list[ExternalIdentity]:
        return list(
            self._session.scalars(select(ExternalIdentity).where(ExternalIdentity.work_id == work_id))
        )

    def update_artwork_local_paths(self, work: Work, local_paths: dict[str, str]) -> None:
        updated: list[dict[str, object]] = []
        for item in work.artwork:
            value = dict(item)
            url = value.get("url")
            if isinstance(url, str) and url in local_paths:
                value["local_path"] = local_paths[url]
            updated.append(value)
        work.artwork = updated
        self._session.flush()

    def set_generated_artwork(
        self,
        work: Work,
        *,
        asset_id: str,
        fanart_path: str,
        poster_path: str,
        timestamp_seconds: float,
    ) -> None:
        """Attach asset-derived artwork without mixing it with provider snapshots."""

        retained = [
            dict(item)
            for item in work.artwork
            if not (
                item.get("source") == "local-screenshot"
                and item.get("asset_id") == asset_id
            )
        ]
        generated = [
            {
                "kind": kind,
                "local_path": path,
                "source": "local-screenshot",
                "asset_id": asset_id,
                "timestamp_seconds": timestamp_seconds,
            }
            for kind, path in (("fanart", fanart_path), ("poster", poster_path))
        ]
        work.artwork = [*generated, *retained]
        self._session.flush()

    def apply_title_translation(
        self,
        work: Work,
        *,
        source: str,
        translated: str,
        provider: str,
    ) -> None:
        sources = dict(work.field_sources or {})
        if work.original_title is None:
            work.original_title = source
            sources["original_title"] = sources.get("title", "unknown")
        work.title = translated
        sources["title"] = f"translation:{provider}"
        work.field_sources = sources
        self._session.flush()

    def _work_has_verified_evidence(self, work_id: str) -> bool:
        remote_snapshot = self._session.scalar(
            select(SourceSnapshot.id)
            .where(
                SourceSnapshot.work_id == work_id,
                SourceSnapshot.provider != "local-path",
            )
            .limit(1)
        )
        if remote_snapshot is not None:
            return True
        accepted_candidate = self._session.scalar(
            select(MatchCandidateRow.id)
            .join(MediaAsset, MatchCandidateRow.asset_id == MediaAsset.id)
            .where(
                MediaAsset.work_id == work_id,
                MatchCandidateRow.provider != "local-path",
                MatchCandidateRow.state == CandidateState.ACCEPTED.value,
            )
            .limit(1)
        )
        return accepted_candidate is not None

    def work_has_verified_evidence(self, work_id: str) -> bool:
        """Return whether a work has accepted metadata from a non-local source."""

        return self._work_has_verified_evidence(work_id)

    def _resolve_existing_work(self, record: ProviderRecord) -> Work | None:
        checks = [(record.provider, IdentityKind.PROVIDER_ID, record.external_id)]
        if record.code:
            checks.append(("global", IdentityKind.CODE, record.code))
        for provider, kind, value in checks:
            normalized = normalize_identity_value(value)
            identity = self._session.scalar(
                select(ExternalIdentity).where(
                    ExternalIdentity.provider == provider,
                    ExternalIdentity.kind == kind.value,
                    ExternalIdentity.normalized_value == normalized,
                )
            )
            if identity is not None:
                return self.get_work(identity.work_id)
        return None

    def _create_work(self, record: ProviderRecord) -> Work:
        category = record.category
        if category is MediaCategory.OTHER:
            category = _category_for_family(record.family.value)
        work = Work(
            title=record.title,
            original_title=record.original_title,
            primary_code=record.code,
            family=record.family.value,
            category=category.value,
            release_date=record.release_date,
            runtime_seconds=record.runtime_seconds,
            studio=record.studio,
            label=record.label,
            series=record.series,
            plot=record.plot,
            actors=list(record.actors),
            directors=list(record.directors),
            tags=list(record.tags),
            artwork=[item.model_dump(mode="json") for item in record.artwork],
            field_sources=_record_field_sources(record),
        )
        self._session.add(work)
        self._session.flush()
        self._sync_work_actors(work)
        return work

    def _merge_local_record(self, work: Work, record: ProviderRecord) -> None:
        # A code-less work must never retain a legacy JAV classification that
        # came only from its library or directory name.  Verified descriptive
        # fields remain intact; only the inferred family/category are repaired.
        if work.primary_code is None:
            work.family = record.family.value
            work.category = record.category.value
        if record.category is not MediaCategory.OTHER:
            work.category = record.category.value
        if work.family == "unknown" and record.family.value != "unknown":
            work.family = record.family.value
        if not work.actors and record.actors:
            work.actors = list(record.actors)
        if work.studio is None and record.studio:
            work.studio = record.studio
        if work.series is None and record.series:
            work.series = record.series
        if not work.tags and record.tags:
            work.tags = list(record.tags)
        self._sync_work_actors(work)

    def _replace_local_record(self, work: Work, record: ProviderRecord) -> None:
        work.title = record.title
        work.original_title = record.original_title
        work.primary_code = record.code
        work.family = record.family.value
        work.category = record.category.value
        work.release_date = record.release_date
        work.runtime_seconds = record.runtime_seconds
        work.studio = record.studio
        work.label = record.label
        work.series = record.series
        work.plot = record.plot
        work.actors = list(record.actors)
        work.directors = list(record.directors)
        work.tags = list(record.tags)
        if record.artwork and not work.artwork:
            work.artwork = [item.model_dump(mode="json") for item in record.artwork]
        work.field_sources = _record_field_sources(record)
        self._sync_work_actors(work)

    def _merge_provider_record(
        self,
        work: Work,
        record: ProviderRecord,
        *,
        overwrite: bool = True,
    ) -> None:
        sources = dict(work.field_sources or {})
        if overwrite or not work.title:
            work.title = record.title
            sources["title"] = record.provider
        if record.original_title and (overwrite or not work.original_title):
            work.original_title = record.original_title
            sources["original_title"] = record.provider
        if record.code and (overwrite or not work.primary_code):
            work.primary_code = record.code
            sources["primary_code"] = record.provider
        if record.family.value != "unknown" and (overwrite or work.family == "unknown"):
            work.family = record.family.value
            sources["family"] = record.provider
        category = record.category
        if category is MediaCategory.OTHER:
            category = _category_for_family(record.family.value)
        if category is not MediaCategory.OTHER and (overwrite or work.category == MediaCategory.OTHER.value):
            work.category = category.value
            sources["category"] = record.provider
        if record.release_date and (overwrite or work.release_date is None):
            work.release_date = record.release_date
            sources["release_date"] = record.provider
        if record.runtime_seconds and (overwrite or work.runtime_seconds is None):
            work.runtime_seconds = record.runtime_seconds
            sources["runtime_seconds"] = record.provider
        if record.studio and (overwrite or not work.studio):
            work.studio = record.studio
            sources["studio"] = record.provider
        if record.label and (overwrite or not work.label):
            work.label = record.label
            sources["label"] = record.provider
        if record.series and (overwrite or not work.series):
            work.series = record.series
            sources["series"] = record.provider
        if record.plot and (overwrite or not work.plot):
            work.plot = record.plot
            sources["plot"] = record.provider

        if record.actors:
            actor_source = str(sources.get("actors", ""))
            incoming_priority = _JAV_ACTOR_PROVIDER_PRIORITY.get(record.provider)
            current_priority = _JAV_ACTOR_PROVIDER_PRIORITY.get(actor_source)
            if incoming_priority is None:
                work.actors = _merge_unique(work.actors, record.actors, replace=overwrite)
            elif current_priority is None or incoming_priority < current_priority:
                work.actors = list(record.actors)
                sources["actors"] = record.provider
            elif incoming_priority == current_priority:
                work.actors = list(record.actors)
        if record.directors:
            work.directors = _merge_unique(work.directors, record.directors, replace=overwrite)
        if record.tags:
            work.tags = _merge_unique(work.tags, record.tags, replace=overwrite)
        if record.artwork and not work.artwork:
            work.artwork = _merge_artwork(work.artwork, record, replace=False)
        work.field_sources = sources
        self._sync_work_actors(work)

    def _sync_work_actors(self, work: Work) -> None:
        existing_links = {
            link.actor_id: link
            for link in self._session.scalars(select(WorkActor).where(WorkActor.work_id == work.id))
        }
        desired_ids: set[str] = set()
        for position, raw_name in enumerate(work.actors):
            name = raw_name.strip()
            normalized = _normalize_actor_name(name)
            if not normalized:
                continue
            actor = self._session.scalar(select(Actor).where(Actor.normalized_name == normalized))
            if actor is None:
                actor = Actor(name=name, normalized_name=normalized)
                self._session.add(actor)
                self._session.flush()
            desired_ids.add(actor.id)
            link = existing_links.get(actor.id)
            if link is None:
                self._session.add(WorkActor(work_id=work.id, actor_id=actor.id, position=position))
            else:
                link.position = position
        for actor_id, link in existing_links.items():
            if actor_id not in desired_ids:
                self._session.delete(link)

    def _add_identity(
        self,
        work: Work,
        *,
        provider: str,
        kind: IdentityKind,
        value: str,
        source_url: str | None = None,
    ) -> None:
        normalized = normalize_identity_value(value)
        existing = self._session.scalar(
            select(ExternalIdentity).where(
                ExternalIdentity.provider == provider,
                ExternalIdentity.kind == kind.value,
                ExternalIdentity.normalized_value == normalized,
            )
        )
        if existing is None:
            self._session.add(
                ExternalIdentity(
                    work_id=work.id,
                    provider=provider,
                    kind=kind.value,
                    value=value,
                    normalized_value=normalized,
                    source_url=source_url,
                )
            )

    def _add_record_identities(self, work: Work, record: ProviderRecord) -> None:
        self._add_identity(
            work,
            provider=record.provider,
            kind=IdentityKind.PROVIDER_ID,
            value=record.external_id,
            source_url=record.source_url,
        )
        if record.code:
            self._add_identity(work, provider="global", kind=IdentityKind.CODE, value=record.code)
        if record.source_url:
            self._add_identity(
                work,
                provider=record.provider,
                kind=IdentityKind.SOURCE_URL,
                value=record.source_url,
                source_url=record.source_url,
            )
        for algorithm, value in record.fingerprints.items():
            self._add_identity(work, provider=algorithm, kind=IdentityKind.FINGERPRINT, value=value)

    def _upsert_snapshot(self, work: Work, record: ProviderRecord) -> None:
        snapshot = self._session.scalar(
            select(SourceSnapshot).where(
                SourceSnapshot.work_id == work.id,
                SourceSnapshot.provider == record.provider,
                SourceSnapshot.external_id == record.external_id,
            )
        )
        payload = record.model_dump(mode="json")
        if snapshot is None:
            self._session.add(
                SourceSnapshot(
                    work_id=work.id,
                    provider=record.provider,
                    external_id=record.external_id,
                    payload=payload,
                )
            )
        else:
            snapshot.payload = payload


def _category_for_family(family: str) -> MediaCategory:
    return {
        "jav": MediaCategory.JAPAN,
        "chinese": MediaCategory.CHINA,
        "korean": MediaCategory.KOREA,
        "western": MediaCategory.EUROPE,
    }.get(family, MediaCategory.OTHER)


def _normalize_actor_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def _record_field_sources(record: ProviderRecord) -> dict[str, str]:
    fields = {
        "title": record.title,
        "original_title": record.original_title,
        "primary_code": record.code,
        "family": record.family.value if record.family.value != "unknown" else None,
        "category": record.category.value if record.category is not MediaCategory.OTHER else None,
        "release_date": record.release_date,
        "runtime_seconds": record.runtime_seconds,
        "studio": record.studio,
        "label": record.label,
        "series": record.series,
        "plot": record.plot,
        "actors": record.actors,
    }
    return {field: record.provider for field, value in fields.items() if value not in {None, ""}}


def _merge_unique(current: list[str], incoming: tuple[str, ...], *, replace: bool) -> list[str]:
    values = [] if replace else list(current)
    seen = {value.casefold() for value in values}
    for value in incoming:
        key = value.casefold()
        if key not in seen:
            seen.add(key)
            values.append(value)
    return values


def _merge_artwork(
    current: list[dict[str, object]],
    record: ProviderRecord,
    *,
    replace: bool,
) -> list[dict[str, object]]:
    values = [] if replace else list(current)
    seen = {(str(item.get("kind")), str(item.get("url"))) for item in values}
    for artwork in record.artwork:
        payload = artwork.model_dump(mode="json")
        key = (str(payload.get("kind")), str(payload.get("url")))
        if key not in seen:
            seen.add(key)
            values.append(payload)
    return values
