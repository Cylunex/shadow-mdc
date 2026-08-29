from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ..domain import IdentityHints, ProviderRecord, ScoredCandidate
from ..enums import AssetState, CandidateState, IdentityKind, MediaCategory
from ..identity import normalize_identity_value
from .models import Base, ExternalIdentity, Library, MatchCandidateRow, MediaAsset, SourceSnapshot, Work


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)
        if self.engine.dialect.name == "sqlite":
            self._migrate_sqlite()

    def _migrate_sqlite(self) -> None:
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
            work_columns = {
                str(row[1]) for row in connection.exec_driver_sql("PRAGMA table_info(works)")
            }
            if "category" not in work_columns:
                connection.exec_driver_sql(
                    "ALTER TABLE works ADD COLUMN category VARCHAR(30) NOT NULL DEFAULT 'Other'"
                )
                connection.exec_driver_sql(
                    "CREATE INDEX IF NOT EXISTS ix_works_category ON works (category)"
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
    ) -> Library:
        library = Library(
            name=name,
            root_path=str(Path(root_path).resolve()),
            category=category.value,
            recursive=recursive,
            organize_template=organize_template,
        )
        self._session.add(library)
        self._session.flush()
        return library

    def list_libraries(self) -> list[Library]:
        return list(self._session.scalars(select(Library).order_by(Library.name)))

    def get_library(self, library_id: str) -> Library | None:
        return self._session.get(Library, library_id)

    def upsert_asset(
        self,
        *,
        library_id: str,
        path: str,
        size: int,
        duration_seconds: float | None,
        oshash: str | None,
        hints: IdentityHints,
    ) -> tuple[MediaAsset, bool]:
        absolute = str(Path(path).resolve())
        existing = self._session.scalar(select(MediaAsset).where(MediaAsset.path == absolute))
        if existing is not None:
            existing.size = size
            existing.duration_seconds = duration_seconds
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
            duration_seconds=duration_seconds,
            oshash=oshash,
            hints=hints.model_dump(mode="json"),
        )
        self._session.add(asset)
        self._session.flush()
        return asset, True

    def get_asset(self, asset_id: str) -> MediaAsset | None:
        return self._session.get(MediaAsset, asset_id)

    def list_assets(self, *, state: str | None = None) -> list[MediaAsset]:
        statement = select(MediaAsset)
        if state is not None:
            statement = statement.where(MediaAsset.state == state)
        else:
            statement = statement.where(MediaAsset.state != AssetState.IGNORED.value)
        return list(self._session.scalars(statement.order_by(MediaAsset.created_at.desc())))

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

    def list_works(self) -> list[Work]:
        return list(self._session.scalars(select(Work).order_by(Work.created_at.desc())))

    def first_asset_for_work(self, work_id: str) -> MediaAsset | None:
        return self._session.scalar(
            select(MediaAsset)
            .where(MediaAsset.work_id == work_id)
            .order_by(MediaAsset.created_at)
            .limit(1)
        )

    def get_work(self, work_id: str) -> Work | None:
        return self._session.get(Work, work_id)

    def identities_for_work(self, work_id: str) -> list[ExternalIdentity]:
        return list(
            self._session.scalars(select(ExternalIdentity).where(ExternalIdentity.work_id == work_id))
        )

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
        )
        self._session.add(work)
        self._session.flush()
        return work

    def _merge_local_record(self, work: Work, record: ProviderRecord) -> None:
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

    def _merge_provider_record(self, work: Work, record: ProviderRecord) -> None:
        work.title = record.title
        if record.original_title:
            work.original_title = record.original_title
        if record.code:
            work.primary_code = record.code
        if record.family.value != "unknown":
            work.family = record.family.value
        if work.category == MediaCategory.OTHER.value:
            category = record.category
            if category is MediaCategory.OTHER:
                category = _category_for_family(record.family.value)
            work.category = category.value
        if record.release_date:
            work.release_date = record.release_date
        if record.runtime_seconds:
            work.runtime_seconds = record.runtime_seconds
        if record.studio:
            work.studio = record.studio
        if record.label:
            work.label = record.label
        if record.series:
            work.series = record.series
        if record.plot:
            work.plot = record.plot
        if record.actors:
            work.actors = list(record.actors)
        if record.directors:
            work.directors = list(record.directors)
        if record.tags:
            work.tags = list(record.tags)
        if record.artwork:
            work.artwork = [item.model_dump(mode="json") for item in record.artwork]

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
