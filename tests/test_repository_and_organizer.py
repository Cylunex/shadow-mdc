from collections.abc import Iterator
from pathlib import Path

import pytest

from shadow_mdc.db.models import Library, MediaAsset
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints, ProviderRecord, ScoredCandidate
from shadow_mdc.enums import ContentFamily, MatchDecision, OperationKind, QueryMode
from shadow_mdc.media.organizer import Organizer


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        yield Repository(session)


def _candidate(record: ProviderRecord) -> ScoredCandidate:
    return ScoredCandidate(record=record, score=1, decision=MatchDecision.ACCEPT, evidence=())


def _asset(
    repository: Repository,
    root: Path,
    *,
    name: str,
) -> tuple[Library, MediaAsset]:
    library = repository.create_library(
        name=f"library-{name}",
        root_path=str(root),
        recursive=True,
        organize_template="{studio}/{code_or_title}/{code_or_title}.{ext}",
    )
    source = root / name
    source.write_bytes(b"fixture")
    hints = IdentityHints(term=name, mode=QueryMode.TEXT, title=name)
    asset, _ = repository.upsert_asset(
        library_id=library.id,
        path=str(source),
        size=source.stat().st_size,
        duration_seconds=None,
        oshash=None,
        hints=hints,
    )
    return library, asset


def test_same_code_from_different_providers_resolves_one_work(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    _, first_asset = _asset(repository, root, name="first.mp4")
    first_record = ProviderRecord(
        provider="source-a",
        external_id="a-1",
        code="SSIS-123",
        title="First source title",
        family=ContentFamily.JAV,
    )
    first_row = repository.save_candidates(first_asset, [_candidate(first_record)])[0]
    first_work = repository.accept_candidate(first_row.id)

    second_library = repository.list_libraries()[0]
    second_source = root / "second.mp4"
    second_source.write_bytes(b"fixture-2")
    second_asset, _ = repository.upsert_asset(
        library_id=second_library.id,
        path=str(second_source),
        size=second_source.stat().st_size,
        duration_seconds=None,
        oshash=None,
        hints=IdentityHints(term="second", mode=QueryMode.TEXT, title="second"),
    )
    second_record = first_record.model_copy(
        update={"provider": "source-b", "external_id": "b-9", "title": "Another title"}
    )
    second_row = repository.save_candidates(second_asset, [_candidate(second_record)])[0]
    second_work = repository.accept_candidate(second_row.id)

    assert first_work.id == second_work.id
    assert len(repository.list_works()) == 1
    assert {(item.provider, item.value) for item in repository.identities_for_work(first_work.id)} >= {
        ("source-a", "a-1"),
        ("source-b", "b-9"),
        ("global", "SSIS-123"),
    }


def test_remote_record_enriches_existing_local_work(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "enrich"
    root.mkdir()
    _, asset = _asset(repository, root, name="SONE-118.mp4")
    local_record = ProviderRecord(
        provider="local-path",
        external_id="local-1",
        code="SONE-118",
        title="SONE-118",
        family=ContentFamily.JAV,
    )
    work = repository.catalog_asset(asset, local_record)
    remote_record = ProviderRecord(
        provider="fixture-remote",
        external_id="remote-118",
        code="SONE-118",
        title="Remote official title",
        family=ContentFamily.JAV,
        studio="Official Studio",
        actors=("河北彩花",),
        tags=("Drama",),
    )
    row = repository.save_candidates(asset, [_candidate(remote_record)])[0]

    enriched = repository.accept_candidate(row.id)

    assert enriched.id == work.id
    assert enriched.title == "Remote official title"
    assert enriched.studio == "Official Studio"
    assert enriched.actors == ["河北彩花"]
    assert enriched.tags == ["Drama"]


def test_failed_remote_refresh_keeps_local_work_identified(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "refresh"
    root.mkdir()
    _, asset = _asset(repository, root, name="ABP-123.mp4")
    repository.catalog_asset(
        asset,
        ProviderRecord(
            provider="local-path",
            external_id="local-abp",
            code="ABP-123",
            title="ABP-123",
            family=ContentFamily.JAV,
        ),
    )

    repository.save_candidates(asset, [])

    assert asset.state == "identified"
    assert asset.error == "no remote candidates"


def test_organizer_requires_current_plan_and_writes_nfo(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "organize"
    root.mkdir()
    library, asset = _asset(repository, root, name="input.mp4")
    record = ProviderRecord(
        provider="fixture",
        external_id="scene-1",
        code="ABP-123",
        title="A title",
        family=ContentFamily.JAV,
        studio="A Studio",
        actors=("Alice",),
        tags=("Drama",),
    )
    row = repository.save_candidates(asset, [_candidate(record)])[0]
    work = repository.accept_candidate(row.id)
    organizer = Organizer(repository)
    plan = organizer.plan(asset=asset, work=work, library=library, mode=OperationKind.COPY)

    with pytest.raises(ValueError, match="plan changed"):
        organizer.execute(
            asset=asset,
            work=work,
            library=library,
            identities=repository.identities_for_work(work.id),
            mode=OperationKind.COPY,
            token="0" * 64,
        )

    organizer.execute(
        asset=asset,
        work=work,
        library=library,
        identities=repository.identities_for_work(work.id),
        mode=OperationKind.COPY,
        token=plan.token,
    )

    destination = root / "A Studio" / "ABP-123" / "ABP-123.mp4"
    assert destination.read_bytes() == b"fixture"
    nfo = destination.with_suffix(".nfo").read_text(encoding="utf-8")
    assert "<title>A title</title>" in nfo
    assert '<uniqueid type="fixture">scene-1</uniqueid>' in nfo


def test_organizer_rejects_absolute_template(repository: Repository, tmp_path: Path) -> None:
    root = tmp_path / "absolute"
    root.mkdir()
    library, asset = _asset(repository, root, name="input.mkv")
    library.organize_template = str((tmp_path / "outside.mkv").resolve())
    record = ProviderRecord(provider="fixture", external_id="1", title="Title")
    row = repository.save_candidates(asset, [_candidate(record)])[0]
    work = repository.accept_candidate(row.id)

    with pytest.raises(ValueError, match="must be relative"):
        Organizer(repository).plan(asset=asset, work=work, library=library, mode=OperationKind.MOVE)
