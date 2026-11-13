from pathlib import Path
from unittest.mock import patch

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.media.nfo_import import (
    NfoImporter,
    NfoImportPolicy,
    resolve_sidecar_nfo,
    sibling_artwork_paths,
)
from shadow_mdc.media.parts import SidecarDirectory
from shadow_mdc.services.scanner import Scanner


def test_flat_scan_only_reads_owned_nfo_and_artwork(tmp_path: Path) -> None:
    folder = tmp_path / "media"
    folder.mkdir()
    for name in ("Film One", "Film Two"):
        (folder / f"{name}.strm").write_text("https://example.com/media", encoding="utf-8")
    generic = folder / "movie.nfo"
    generic.write_text("<movie><title>Incorrect</title><lockdata>true</lockdata></movie>", encoding="utf-8")
    specific = folder / "Film One.nfo"
    specific.write_text("<movie><title>Film One</title></movie>", encoding="utf-8")
    (folder / "poster.jpg").write_bytes(b"generic")
    (folder / "Film One-poster.jpg").write_bytes(b"specific")
    (folder / "fanart.jpg").write_bytes(b"generic fanart")
    database = Database(f"sqlite:///{tmp_path / 'db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        library = repo.create_library(
            name="test", root_path=str(folder), recursive=True, organize_template="{title}"
        )
        scanner = Scanner(repo, artwork_dir=tmp_path / "art")
        with patch.object(SidecarDirectory, "inspect", side_effect=AssertionError("redundant listdir")):
            assert not scanner.scan(library).errors
        (work,) = repo.list_works()
        assert work.title == "Film One" and not work.field_locks
        cached = work.artwork[0]["local_path"]
        assert isinstance(cached, str) and Path(cached).read_bytes() == b"specific"
        assert [image["kind"] for image in work.artwork] == ["poster"]
        assets = {Path(asset.path).stem: asset for asset in repo.list_assets()}
        assert assets["Film One"].work_id == work.id
        assert assets["Film Two"].work_id is None
        specific.unlink()
        for mode in ("trust", "candidate"):
            result = NfoImporter(repo, NfoImportPolicy(mode=mode)).import_library_assets(
                list(assets.values())
            )
            assert result.scanned == 0
        assert not scanner.scan(library).errors
        assert work.title == "Film One" and not work.field_locks
        (folder / "Film Two.strm").unlink()
        stale = assets["Film Two"]
        assert NfoImporter(repo, NfoImportPolicy(mode="trust")).import_library_assets([stale]).scanned == 0
        assert stale.work_id is None


@pytest.mark.parametrize(
    ("names", "shared"),
    [
        (["Film.mp4"], True),
        (["Film-CD1.mkv", "Film-cd2.strm", "Film-cd3.mp4"], True),
        (["日本映画-Part01.mkv", "日本映画-Part02.mp4"], True),
        (["Film-CD1.mp4", "Film-CD3.mkv"], False),
        (["First-CD1.mp4", "Other-CD2.mkv"], False),
        (["Film-CD1.mkv", "Film-CD1.strm"], False),
        (["First.mkv", "First 2.mp4"], False),
    ],
)
def test_directory_sidecars_require_one_work(tmp_path: Path, names: list[str], shared: bool) -> None:
    for name in names:
        (tmp_path / name).touch()
    shared_nfo = tmp_path / "movie.nfo"
    shared_nfo.touch()
    art = tmp_path / "fanart.png"
    art.touch()
    for name in names:
        assert resolve_sidecar_nfo(tmp_path / name) == (shared_nfo if shared else None)
        assert sibling_artwork_paths(tmp_path / name) == ({"fanart": art} if shared else {})


def test_mixed_directory_reserves_generic_name_and_colliding_stems(tmp_path: Path) -> None:
    for name in (
        "Movie.mp4",
        "Film.mkv",
        "Film.strm",
        "Feature.mkv",
        "movie.nfo",
        "Movie.nfo",
        "Film.nfo",
        "Film-poster.jpg",
        "Feature.nfo",
        "Feature-poster.jpg",
    ):
        (tmp_path / name).touch()
    for name in ("Movie.mp4", "Film.mkv", "Film.strm"):
        assert resolve_sidecar_nfo(tmp_path / name) is None
        assert not sibling_artwork_paths(tmp_path / name)
    assert resolve_sidecar_nfo(tmp_path / "Feature.mkv") == tmp_path / "Feature.nfo"
    assert sibling_artwork_paths(tmp_path / "Feature.mkv") == {"poster": tmp_path / "Feature-poster.jpg"}


def test_disconnected_media_link_reserves_its_directory(tmp_path: Path) -> None:
    media = tmp_path / "local.strm"
    media.touch()
    (tmp_path / "movie.nfo").touch()
    link = tmp_path / "other.mkv"
    link.symlink_to(tmp_path / "disconnected")
    assert resolve_sidecar_nfo(media) is None
    link.unlink()
    assert resolve_sidecar_nfo(media) == tmp_path / "movie.nfo"


def test_unreadable_directory_disables_sidecar_claims(tmp_path: Path) -> None:
    media = tmp_path / "Film.mp4"
    media.touch()
    (tmp_path / "Film.nfo").touch()
    (tmp_path / "poster.jpg").touch()
    with patch("os.scandir", side_effect=PermissionError):
        assert resolve_sidecar_nfo(media) is None
        assert not sibling_artwork_paths(media)


def test_media_symlink_cannot_complete_a_letter_volume_group(tmp_path: Path) -> None:
    directory = tmp_path / "TEST-123"
    directory.mkdir()
    media = directory / "A.strm"
    media.touch()
    (directory / "B.mkv").symlink_to(directory / "missing-volume")
    (directory / "movie.nfo").touch()
    layout = SidecarDirectory.inspect(directory)
    assert not layout.shared and layout.volume_code is None
    assert resolve_sidecar_nfo(media, layout) is None
