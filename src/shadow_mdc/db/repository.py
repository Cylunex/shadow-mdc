from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import Engine, create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from ..domain import IdentityHints, ProviderRecord, ScoredCandidate
from ..enums import AssetState, CandidateState, IdentityKind
from ..identity import normalize_identity_value
from .models import Base, ExternalIdentity, Library, MatchCandidateRow, MediaAsset, SourceSnapshot, Work


class Database:
    def __init__(self, url: str):
        connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
        self.engine: Engine = create_engine(url, connect_args=connect_args)
        self._sessions = sessionmaker(self.engine, expire_on_commit=False)

    def initialize(self) -> None:
        Base.metadata.create_all(self.engine)

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
        recursive: bool,
        organize_template: str,
    ) -> Library:
        library = Library(
            name=name,
            root_path=str(Path(root_path).resolve()),
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
        return list(self._session.scalars(statement.order_by(MediaAsset.created_at.desc())))

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

    def list_works(self) -> list[Work]:
        return list(self._session.scalars(select(Work).order_by(Work.created_at.desc())))

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
        work = Work(
            title=record.title,
            original_title=record.original_title,
            primary_code=record.code,
            family=record.family.value,
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
