from pydantic import BaseModel, ConfigDict

from ..db.models import MatchCandidateRow, Work
from ..db.repository import Repository
from ..domain import IdentityHints
from ..enums import MatchDecision
from ..identity import normalize_identity_value
from ..matching import rank_candidates
from ..providers.base import ProviderFailure, ProviderRegistry, SearchBatch


class IdentifyResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    asset_id: str
    candidate_ids: tuple[str, ...]
    accepted_work_id: str | None
    failures: tuple[ProviderFailure, ...]


class IdentifyService:
    def __init__(self, repository: Repository, providers: ProviderRegistry):
        self._repository = repository
        self._providers = providers

    async def identify(self, asset_id: str) -> IdentifyResult:
        asset = self._repository.get_asset(asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        hints = IdentityHints.model_validate(asset.hints)
        if hints.code:
            existing = self._repository.find_work_by_code(hints.code)
            if existing is not None:
                self._repository.attach_asset_to_work(asset, existing)
                return IdentifyResult(
                    asset_id=asset.id,
                    candidate_ids=(),
                    accepted_work_id=existing.id,
                    failures=(),
                )
        batch = await self._providers.search(hints)
        return self.apply_batch(asset_id, batch)

    def apply_batch(self, asset_id: str, batch: SearchBatch) -> IdentifyResult:
        asset = self._repository.get_asset(asset_id)
        if asset is None:
            raise LookupError(f"asset {asset_id} not found")
        hints = IdentityHints.model_validate(asset.hints)
        ranked = rank_candidates(hints, list(batch.records))
        rows = self._repository.save_candidates(asset, ranked)
        accepted: Work | None = None
        if ranked and ranked[0].decision is MatchDecision.ACCEPT:
            first_row = _find_row(rows, ranked[0].record.provider, ranked[0].record.external_id)
            if first_row is not None:
                accepted = self._repository.accept_candidate(first_row.id)
                primary_code = ranked[0].record.code
                if primary_code:
                    for candidate in ranked[1:]:
                        if candidate.decision is not MatchDecision.ACCEPT:
                            continue
                        if not candidate.record.code or normalize_identity_value(
                            candidate.record.code
                        ) != normalize_identity_value(primary_code):
                            continue
                        row = _find_row(
                            rows,
                            candidate.record.provider,
                            candidate.record.external_id,
                        )
                        if row is not None:
                            accepted = self._repository.merge_candidate_into_work(
                                row.id,
                                accepted.id,
                            )
        return IdentifyResult(
            asset_id=asset.id,
            candidate_ids=tuple(row.id for row in rows),
            accepted_work_id=accepted.id if accepted else None,
            failures=batch.failures,
        )


def _find_row(rows: list[MatchCandidateRow], provider: str, external_id: str) -> MatchCandidateRow | None:
    return next(
        (row for row in rows if row.provider == provider and row.external_id == external_id),
        None,
    )
