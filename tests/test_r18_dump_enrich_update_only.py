from __future__ import annotations

from pathlib import Path

from shadow_mdc.db.models import Work
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily
from shadow_mdc.services.r18_dump import PROVIDER_ID


def test_merge_provider_into_work_does_not_create(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 't.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        existing = Work(title="Existing", primary_code="SSIS-001", family=ContentFamily.JAV.value)
        session.add(existing)
        session.flush()
        before = len(repo.list_works())
        record = ProviderRecord(
            provider=PROVIDER_ID,
            external_id="ssis001",
            source_url="https://r18.dev/x",
            code="SSIS-999",  # different code — upsert would create; merge must not
            title="From dump",
            original_title="From dump",
            family=ContentFamily.JAV,
            studio="Maker",
        )
        merged = repo.merge_provider_into_work(existing, record, overwrite=False)
        session.commit()
        assert merged.id == existing.id
        assert merged.studio == "Maker"
        assert len(repo.list_works()) == before


def test_upsert_still_creates_when_unresolved(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 't.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        record = ProviderRecord(
            provider=PROVIDER_ID,
            external_id="brandnew",
            source_url="https://r18.dev/y",
            code="ZZZZ-001",
            title="Brand new",
            family=ContentFamily.JAV,
        )
        created = repo.upsert_provider_record(record, overwrite=False)
        session.commit()
        assert created.primary_code == "ZZZZ-001"
        assert len(repo.list_works()) == 1
