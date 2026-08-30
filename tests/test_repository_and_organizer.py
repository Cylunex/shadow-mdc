from collections.abc import Iterator
from pathlib import Path

import pytest

from shadow_mdc.db.models import Library, MediaAsset
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints, ProviderRecord, ScoredCandidate
from shadow_mdc.enums import ContentFamily, MatchDecision, NfoPolicy, OutputMode, QueryMode
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
    plan = organizer.plan(
        asset=asset,
        work=work,
        library=library,
        mode=OutputMode.COPY,
        target_root=str(root),
    )

    with pytest.raises(ValueError, match="plan changed"):
        organizer.execute(
            asset=asset,
            work=work,
            library=library,
            identities=repository.identities_for_work(work.id),
            mode=OutputMode.COPY,
            token="0" * 64,
            target_root=str(root),
        )

    organizer.execute(
        asset=asset,
        work=work,
        library=library,
        identities=repository.identities_for_work(work.id),
        mode=OutputMode.COPY,
        token=plan.token,
        target_root=str(root),
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
        Organizer(repository).plan(
            asset=asset,
            work=work,
            library=library,
            mode=OutputMode.MOVE,
            target_root=str(root),
        )


def test_sidecar_mode_writes_nfo_without_moving_media(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "sidecar"
    root.mkdir()
    library, asset = _asset(repository, root, name="SONE-118.strm")
    record = ProviderRecord(
        provider="fixture",
        external_id="sone-118",
        code="SONE-118",
        title="Sidecar title",
        family=ContentFamily.JAV,
    )
    work = repository.accept_candidate(repository.save_candidates(asset, [_candidate(record)])[0].id)
    organizer = Organizer(repository)
    plan = organizer.plan(asset=asset, work=work, library=library, mode=OutputMode.SIDECAR)

    assert [operation.kind.value for operation in plan.operations] == ["write_nfo"]
    organizer.execute(
        asset=asset,
        work=work,
        library=library,
        identities=repository.identities_for_work(work.id),
        mode=OutputMode.SIDECAR,
        token=plan.token,
        nfo_policy=NfoPolicy.REPLACE,
    )

    assert (root / "SONE-118.strm").read_bytes() == b"fixture"
    assert "<title>Sidecar title</title>" in (root / "SONE-118.nfo").read_text(encoding="utf-8")


def test_copy_mode_keeps_parts_distinct_and_copies_matching_subtitle(
    repository: Repository,
    tmp_path: Path,
) -> None:
    source_root = tmp_path / "parts"
    target_root = tmp_path / "library"
    source_root.mkdir()
    target_root.mkdir()
    library, asset = _asset(repository, source_root, name="ABP-123-CD2.mp4")
    subtitle = source_root / "ABP-123-CD2.zh.srt"
    subtitle.write_text("subtitle", encoding="utf-8")
    record = ProviderRecord(
        provider="fixture",
        external_id="abp-123",
        code="ABP-123",
        title="Multipart",
        family=ContentFamily.JAV,
        studio="Studio",
    )
    work = repository.accept_candidate(repository.save_candidates(asset, [_candidate(record)])[0].id)
    organizer = Organizer(repository)
    plan = organizer.plan(
        asset=asset,
        work=work,
        library=library,
        mode=OutputMode.COPY,
        target_root=str(target_root),
    )

    destinations = [Path(operation.destination).name for operation in plan.operations]
    assert destinations == ["ABP-123-CD2.mp4", "ABP-123-CD2.zh.srt", "ABP-123-CD2.nfo"]
    organizer.execute(
        asset=asset,
        work=work,
        library=library,
        identities=repository.identities_for_work(work.id),
        mode=OutputMode.COPY,
        target_root=str(target_root),
        token=plan.token,
    )

    destination_dir = target_root / "Studio" / "ABP-123"
    assert (destination_dir / "ABP-123-CD2.mp4").is_file()
    assert (destination_dir / "ABP-123-CD2.zh.srt").read_text(encoding="utf-8") == "subtitle"


def test_sidecar_plan_copies_cached_artwork_next_to_media(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "artwork-sidecar"
    root.mkdir()
    library, asset = _asset(repository, root, name="ABP-123.mp4")
    cached = tmp_path / "cache.jpg"
    cached.write_bytes(b"image")
    record = ProviderRecord(
        provider="fixture",
        external_id="abp-123",
        code="ABP-123",
        title="Artwork",
        family=ContentFamily.JAV,
    )
    work = repository.accept_candidate(repository.save_candidates(asset, [_candidate(record)])[0].id)
    work.artwork = [
        {
            "url": "https://images.example/cover.jpg",
            "kind": "thumb",
            "local_path": str(cached),
        }
    ]
    organizer = Organizer(repository)
    plan = organizer.plan(asset=asset, work=work, library=library, mode=OutputMode.SIDECAR)

    assert [Path(item.destination).name for item in plan.operations] == [
        "ABP-123-poster.jpg",
        "ABP-123.nfo",
    ]
    organizer.execute(
        asset=asset,
        work=work,
        library=library,
        identities=repository.identities_for_work(work.id),
        mode=OutputMode.SIDECAR,
        token=plan.token,
    )
    assert (root / "ABP-123-poster.jpg").read_bytes() == b"image"
