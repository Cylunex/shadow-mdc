"""Offline regression for filename evidence and conflicting metadata on ingestion."""

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import patch

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints, ProviderRecord
from shadow_mdc.enums import MatchDecision, QueryMode
from shadow_mdc.filename_tags import filename_tags
from shadow_mdc.matching import score_candidate
from shadow_mdc.media.nfo_import import NfoImporter, NfoImportPolicy
from shadow_mdc.providers.base import ProviderRegistry, SearchBatch
from shadow_mdc.services.identify import IdentifyService
from shadow_mdc.services.scanner import Scanner, _discover_files
from shadow_mdc.tags import tag_id


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    database = Database(f"sqlite:///{tmp_path / 'library.db'}")
    database.initialize()
    with database.session() as session:
        yield Repository(session)


@pytest.mark.parametrize("name", [
    "SSIS-123-LTX.mp4", "SSIS-123_ltx.strm", "SSIS-123.LtX.mkv",
    "SSIS-123-LTX-1080p.mp4", r"C:\LTX\SSIS-123-LTX.mp4",
])
def test_filename_release_marker(name: str) -> None:
    assert filename_tags(name) == ("LTX",)


@pytest.mark.parametrize("name", [
    "LTX-123.mp4", "SSIS-123-LTXA.mp4", "SSIS-123-L.mp4", "SSIS-123-LC.mp4",
    "/SSIS-123-LTX/SSIS-123.mp4", r"C:\SSIS-123-LTX\SSIS-123.strm", "SSIS-123.LTX2.mp4",
])
def test_filename_near_matches_are_not_release_markers(name: str) -> None:
    assert filename_tags(name) == ()


@pytest.mark.parametrize("strong_hint", ["fingerprint", "external_id", "source_url", "descriptive"])
def test_conflicting_code_cannot_auto_accept_even_with_matching_other_evidence(strong_hint: str) -> None:
    hints = IdentityHints(
        term="SSIS-123", mode=QueryMode.CODE, code="SSIS-123", title="Same title",
        studio="Studio", series="Series", actors=("Actor 1", "Actor 2"), duration_seconds=3600,
        fingerprints={"oshash": "fixture"} if strong_hint == "fingerprint" else {},
        external_ids={"fixture": "one"} if strong_hint == "external_id" else {},
        source_url="https://example.com/one" if strong_hint == "source_url" else None,
    )
    record = ProviderRecord(
        provider="fixture", external_id="one", code="SSIS-999", title="Same title",
        studio="Studio", series="Series", actors=hints.actors, runtime_seconds=3600,
        fingerprints=hints.fingerprints, source_url=hints.source_url,
    )
    candidate = score_candidate(hints, record)
    assert candidate.decision is MatchDecision.REJECT
    assert candidate.score == 0
    assert candidate.evidence[0].kind == "code_conflict"


@pytest.mark.parametrize("code", [
    "ssis_123", "SSIS123", "\uff33\uff33\uff29\uff33\uff0d\uff11\uff12\uff13",
])
def test_equivalent_code_formats_still_accept(code: str) -> None:
    hints = IdentityHints(term="SSIS-123", code="SSIS-123", mode=QueryMode.CODE)
    record = ProviderRecord(provider="fixture", external_id="one", code=code, title="Title")
    assert score_candidate(hints, record).decision is MatchDecision.ACCEPT


def test_filename_tags_survive_accept_rescrape_and_respect_locks(
    tmp_path: Path, repository: Repository,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    path = root / "SSIS-123-LTX.strm"
    path.write_text("https://example.com/movie.mp4", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{code_or_title}.{ext}")
    Scanner(repository).scan(library)
    asset = repository.list_assets()[0]
    local = repository.local_candidate_record(asset.id)
    assert local is not None and "LTX" in local.tags
    remote = ProviderRecord(provider="fixture", external_id="one", code="SSIS-123",
                            title="Remote title", tags=("丝袜",))
    candidate = score_candidate(IdentityHints.model_validate(asset.hints), remote)
    work = repository.accept_candidate(repository.save_candidates(asset, [candidate])[0].id)
    assert {tag_id(tag) for tag in work.tags} == {tag_id("LTX"), tag_id("丝袜")}
    repository.upsert_provider_record(remote.model_copy(update={"tags": ("黑丝",)}), overwrite=True)
    assert {tag_id(tag) for tag in work.tags} == {tag_id("LTX"), tag_id("黑丝")}
    # Normal rescan fills an older catalog's marker without replacing source metadata.
    work.tags = ["丝袜"]
    Scanner(repository).scan(library)
    assert work.title == "Remote title"
    assert "LTX" in work.tags
    assert work.tags.count("LTX") == 1
    repository.update_work_fields(work, tags=["丝袜"])
    Scanner(repository).scan(library)
    repository.upsert_provider_record(remote, overwrite=True)
    assert work.tags == ["丝袜"]


@pytest.mark.parametrize("mode", ["trust", "candidate"])
def test_nfo_code_conflict_is_diagnostic_and_does_not_create_work(
    tmp_path: Path, repository: Repository, mode: str,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    path = root / "SSIS-123.strm"
    path.write_text("https://example.com/movie.mp4", encoding="utf-8")
    nfo = root / "movie.nfo"
    nfo.write_text("<movie><num>SSIS-999</num><title>Wrong title</title></movie>", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{code_or_title}.{ext}")
    scan = Scanner(repository).scan(library)
    assert scan.queued == 1
    assert "NFO code conflict" in scan.errors[0]
    asset = repository.list_assets()[0]
    result = NfoImporter(repository, NfoImportPolicy(mode=mode)).import_library_assets([asset])
    assert result.scanned == 1 and result.trusted == 0 and result.candidates == 0
    assert "expected=SSIS-123; received=SSIS-999" in result.errors[0]
    assert repository.list_works() == []
    assert asset.work_id is None
    assert "Wrong title" in nfo.read_text(encoding="utf-8")


def test_wrong_nfo_cannot_import_manual_fields_into_existing_work(
    tmp_path: Path, repository: Repository,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "SSIS-123.strm").write_text("https://example.com/movie.mp4", encoding="utf-8")
    work = repository.upsert_provider_record(
        ProviderRecord(provider="fixture", external_id="one", code="SSIS-123", title="Correct"),
        overwrite=False,
    )
    (root / "movie.nfo").write_text(
        "<movie><num>SSIS-999</num><title>Wrong</title><lockdata>true</lockdata></movie>",
        encoding="utf-8",
    )
    (root / "poster.jpg").write_bytes(b"untrusted artwork")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{code_or_title}.{ext}")
    result = Scanner(repository, artwork_dir=tmp_path / "artwork").scan(library)
    assert len(result.errors) == 1
    assert work.title == "Correct" and not work.field_locks
    assert not work.artwork and not (tmp_path / "artwork").exists()
    assert repository.list_assets()[0].work_id == work.id


def test_scan_skips_auxiliary_directories_but_keeps_similarly_named_features(
    tmp_path: Path, repository: Repository,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    for directory in ("backdrops", "Trailers", "extras", "featurettes", "interviews", "deleted scenes"):
        folder = root / directory
        folder.mkdir()
        (folder / "SSIS-999.strm").write_text("not a valid stream", encoding="utf-8")
    feature = root / "Extras collection"
    feature.mkdir()
    (feature / "SSIS-123.strm").write_text("https://example.com/movie.mp4", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{code_or_title}.{ext}")
    result = Scanner(repository).scan(library)
    assert result.discovered == 1 and not result.errors
    assert Path(repository.list_assets()[0].path).parent == feature


def test_missing_root_keeps_registered_assets_and_works(
    tmp_path: Path, repository: Repository,
) -> None:
    root = tmp_path / "disk"
    root.mkdir()
    (root / "SSIS-123.strm").write_text("https://example.com/movie.mp4", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{code_or_title}.{ext}")
    Scanner(repository).scan(library)
    asset = repository.list_assets()[0]
    work = repository.accept_local_candidate(asset.id)
    assert work is not None
    root.rename(tmp_path / "disconnected")
    with pytest.raises(ValueError, match="unavailable"):
        Scanner(repository).scan(library)
    assert repository.get_asset(asset.id) is asset
    assert repository.get_work(work.id) is work


def test_identification_cannot_write_conflicting_candidate_metadata(
    tmp_path: Path, repository: Repository,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "SSIS-123.strm").write_text("https://example.com/movie.mp4", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{code_or_title}.{ext}")
    Scanner(repository).scan(library)
    asset = repository.list_assets()[0]
    service = IdentifyService(repository, ProviderRegistry([]))
    correct = ProviderRecord(provider="correct", external_id="one", code="SSIS-123", title="Correct")
    conflicting = correct.model_copy(update={"provider": "wrong", "code": "SSIS-999", "title": "Wrong"})
    result = service.apply_batch(asset.id, SearchBatch(records=(conflicting, correct), failures=()))
    assert result.accepted_work_id == asset.work_id
    assert len(repository.list_works()) == 1
    work = repository.get_work(asset.work_id or "")
    assert work is not None and work.title == "Correct"
    assert {item.provider for item in repository.identities_for_work(work.id)} == {"correct", "global"}


@pytest.mark.parametrize("marker", [".nomedia", ".ignore"])
def test_directory_marker_preserves_existing_assets_and_can_be_removed(
    tmp_path: Path, repository: Repository, marker: str,
) -> None:
    root = tmp_path / "media"
    folder = root / "private"
    nested = folder / "nested"
    nested.mkdir(parents=True)
    media = nested / "TEST-001.strm"
    media.write_text("https://example.com/video.mp4", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{title}")
    scanner = Scanner(repository)
    assert scanner.scan(library).discovered == 1
    existing = repository.list_assets()[0]
    work = repository.accept_local_candidate(existing.id)
    assert work is not None
    (folder / marker).touch()
    (folder / "TEST-002.strm").write_text("https://example.com/video.mp4", encoding="utf-8")
    (root / "TEST-003.strm").write_text("https://example.com/video.mp4", encoding="utf-8")
    result = scanner.scan(library)
    assert (result.discovered, result.updated, result.errors) == (1, 0, ())
    assert repository.get_asset(existing.id) is existing and existing.work_id == work.id
    assert media.is_file()
    (folder / marker).unlink()
    assert scanner.scan(library, only_new=True).discovered == 1


@pytest.mark.parametrize("marker_kind,discovered", [
    ("empty", 0), ("rules", 1), ("directory", 1), ("symlink", 1),
])
def test_root_ignore_marker_requires_empty_regular_file(
    tmp_path: Path, repository: Repository, marker_kind: str, discovered: int,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    marker = root / ".ignore"
    if marker_kind == "directory":
        marker.mkdir()
    elif marker_kind == "symlink":
        target = tmp_path / "empty"
        target.touch()
        marker.symlink_to(target)
    else:
        marker.write_text("*.strm" if marker_kind == "rules" else "", encoding="utf-8")
    (root / "TEST-001.strm").write_text("https://example.com/video.mp4", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=False,
                                        organize_template="{title}")
    result = Scanner(repository).scan(library, only_new=True)
    assert result.discovered == discovered and not result.errors


def test_discovery_uses_one_directory_listing_and_no_media_stats(tmp_path: Path) -> None:
    merge = tmp_path / "TEST-001"
    veto = tmp_path / "TEST-002"
    excluded = tmp_path / "private"
    nested = excluded / "nested"
    nested.mkdir(parents=True)
    (excluded / ".nomedia").touch()
    for folder in (merge, veto):
        folder.mkdir()
        for part in ("A.strm", "B.strm"):
            (folder / part).touch()
    # A per-part NFO symlink still vetoes merging, even though discovery skips symlinked files.
    target = tmp_path / "info.xml"
    target.touch()
    (veto / "TEST-002A.nfo").symlink_to(target)
    errors: list[str] = []
    with (
        patch("os.scandir", wraps=os.scandir) as scandir,
        patch.object(Path, "iterdir", side_effect=AssertionError("redundant directory listing")),
        patch.object(Path, "stat", side_effect=AssertionError("redundant path stat")),
    ):
        paths, codes = _discover_files(tmp_path, recursive=True, errors=errors)
    assert not errors
    assert codes == {merge: "TEST-001"}
    assert set(paths) == {target, merge / "A.strm", merge / "B.strm", veto / "A.strm", veto / "B.strm"}
    listed = [call.args[0] for call in scandir.call_args_list]
    assert sorted(listed) == sorted((tmp_path, merge, veto, excluded))


@pytest.mark.parametrize("xml", [
    "<movie><title>Truncated",
    "<tvshow><title>Wrong type</title><lockdata>true</lockdata></tvshow>",
    "<?xml version='1.0' encoding='unsupported-codec'?><movie/>",
])
def test_bad_nfo_reports_path_on_rescan_and_recovers_after_correction(
    tmp_path: Path, repository: Repository, xml: str,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    (root / "TEST-001.strm").write_text("https://example.com/movie.mp4", encoding="utf-8")
    nfo = root / "movie.nfo"
    nfo.write_text("<movie><id>TEST-001</id><title>Initial</title></movie>", encoding="utf-8")
    library = repository.create_library(name="test", root_path=str(root), recursive=True,
                                        organize_template="{title}")
    scanner = Scanner(repository, artwork_dir=tmp_path / "artwork")
    assert not scanner.scan(library).errors
    work = repository.list_works()[0]
    nfo.write_text(xml, encoding="utf-8")
    (root / "poster.jpg").write_bytes(b"untrusted artwork")
    errors = scanner.scan(library).errors
    assert len(errors) == 1 and str(nfo) in errors[0] and "NFO" in errors[0]
    assert work.title == "Initial" and not work.artwork and not work.field_locks
    nfo.write_text("<movie><id>TEST-001</id><title>Repaired</title><lockdata>true</lockdata></movie>",
                   encoding="utf-8")
    assert not scanner.scan(library).errors
    assert work.title == "Repaired" and "title" in work.field_locks
