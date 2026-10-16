"""Directory letter-volume merge + obsolete Emby-export cleanup (miyabi-inspired)."""

from __future__ import annotations

from pathlib import Path

import pytest

from shadow_mdc.media.parts import directory_volume_merge_code, parse_letter_volume
from shadow_mdc.normalize_code import is_format_equivalent
from shadow_mdc.services.obsolete_exports import ObsoleteExportQueue
from shadow_mdc.services.pan import PanSettings, RemoteVideo
from shadow_mdc.services.strm_export import (
    export_work,
    find_export_dirs_for_code,
    plan_strm_entries,
    remove_export_dirs_for_code,
    write_sidecar,
)


@pytest.mark.parametrize(
    ("left", "right", "want"),
    [
        ("ABC-001", "ABC1", True),
        ("ABC00123", "ABC-123", True),
        ("IPX_052", "ipx-52", True),
        ("FJIN-106", "FJIN-106A", False),
        ("FJIN-106A", "FJIN-106B", False),
        ("", "ABC-123", False),
    ],
)
def test_is_format_equivalent_padding_and_letter_variants(left: str, right: str, want: bool) -> None:
    assert is_format_equivalent(left, right) is want


@pytest.mark.parametrize(
    ("directory", "files", "sidecars", "want"),
    [
        ("onsd-340", ["onsd-340B.mp4", "onsd-340A.mp4"], None, "ONSD-340"),
        ("ONSD-340", ["a.mp4", "B.MKV", "c.avi"], None, "ONSD-340"),
        ("ONSD-340", ["ONSD-340-A.avi", "ONSD-340-B.avi"], None, "ONSD-340"),
        ("ONSD-340", ["onsd340_a.mp4", "onsd340_b.mp4"], None, "ONSD-340"),
        ("ONSD-340", ["ONSD-340.A.wmv", "ONSD-340.B.wmv"], None, "ONSD-340"),
        ("ONSD-340", ["ONSD-340 A.mp4", "ONSD-340 B.mp4"], None, "ONSD-340"),
        ("ONSD-340", ["ONSD00340A.mp4", "ONSD00340B.mp4"], None, "ONSD-340"),
        ("060326_001", ["060326_001A.mp4", "060326_001B.mp4"], None, "060326-001"),
        ("ONSD-340", ["A.mp4", "B.mp4"], ["onsd340.nfo"], "ONSD-340"),
        ("ONSD-340", ["A.mp4", "B.mp4"], ["movie.nfo"], "ONSD-340"),
        ("Movies", ["FJIN-106A.mp4", "FJIN-106B.mp4"], None, None),
        ("ONSD-340", ["FJIN-106A.mp4", "FJIN-106B.mp4"], None, None),
        ("ONSD-340 FJIN-106", ["A.mp4", "B.mp4"], None, None),
        ("ONSD-340", ["ONSD-340A.mp4"], None, None),
        ("ONSD-340", ["ONSD-340B.mp4", "ONSD-340C.mp4"], None, None),
        ("ONSD-340", ["A.mp4", "C.mp4"], None, None),
        ("ONSD-340", ["A.mp4", "A.mkv", "B.mp4"], None, None),
        ("ONSD-340", ["A.mp4", "B.mp4", "FJIN-106.mp4"], None, None),
        ("ONSD-340", ["A.mp4", "B.mp4", "feature.mp4"], None, None),
        ("ONSD-340", ["ONSD-340A.mp4", "B.mp4"], None, None),
        ("ONSD-340", ["ONSD-340_A.mp4", "ONSD-340-B.mp4"], None, None),
        ("FJIN-106", ["FJIN-106A.mp4", "FJIN-106B.mp4"], ["FJIN-106A.nfo"], None),
        ("ONSD-340", ["A.mp4", "B.mp4"], ["FJIN-106.nfo"], None),
    ],
)
def test_directory_volume_merge_code_table(
    directory: str,
    files: list[str],
    sidecars: list[str] | None,
    want: str | None,
) -> None:
    assert directory_volume_merge_code(directory, files, sidecars or ()) == want


def test_directory_volume_merge_ignores_undersized_trailers() -> None:
    assert (
        directory_volume_merge_code(
            "ONSD-340",
            ["ONSD-340A.mp4", "ONSD-340B.mp4", "trailer.mp4"],
            sizes=[100, 100, 5],
            min_video_bytes=50,
        )
        == "ONSD-340"
    )
    assert (
        directory_volume_merge_code(
            "ONSD-340",
            ["ONSD-340A.mp4", "ONSD-340B.mp4", "trailer.mp4"],
            sizes=[100, 5, 5],
            min_video_bytes=50,
        )
        is None
    )


def test_plan_strm_entries_orders_letter_volumes() -> None:
    entries = plan_strm_entries(
        "ONSD-340",
        [
            RemoteVideo("2", "ONSD-340B.mp4", size=1000, relative_path="b.mp4"),
            RemoteVideo("1", "ONSD-340A.mp4", size=1000, relative_path="a.mp4"),
        ],
    )
    assert [entry.name for entry in entries] == ["ONSD-340-cd1.strm", "ONSD-340-cd2.strm"]
    assert [entry.file_id for entry in entries] == ["1", "2"]


def test_parse_letter_volume_bare_and_suffixed() -> None:
    bare = parse_letter_volume("b")
    assert bare is not None and bare.letter == "B" and bare.base == ""
    dashed = parse_letter_volume("ONSD-340-A")
    assert dashed is not None and dashed.letter == "A" and dashed.base == "ONSD-340"


def test_obsolete_export_queue_removes_absent_codes_and_retries(
    tmp_path: Path,
) -> None:
    root = tmp_path / "export"
    settings = PanSettings(
        strm_enabled=True,
        strm_output_root=str(root),
        strm_mode="relay",
        strm_public_base_url="http://nas.test",
        strm_layout_template="{code}",
    )
    # Seed two managed export folders (letter-variant codes).
    for code in ("ONSD-340A", "ONSD-340B"):
        export_work(
            settings=settings,
            code=code,
            videos=[RemoteVideo("f", f"{code}.mp4", size=10, relative_path=f"{code}.mp4")],
        )
    queue = ObsoleteExportQueue(tmp_path / "obsolete.json")
    queue.mark(["ONSD-340A", "ONSD-340B"])

    present = {"ONSD-340A"}
    notifications: list[tuple[list[str], str]] = []

    def notify(paths, kind: str) -> None:
        notifications.append((list(paths), kind))

    # First drain: A still in library → skip (drop from queue); B removed + notified.
    result = queue.drain(
        settings=settings,
        code_exists=lambda code: code in present,
        notify=notify,
    )
    assert "ONSD-340A" in result.skipped_still_present
    assert any(path.name == "ONSD-340B" for path in result.removed)
    assert queue.codes == ()
    assert find_export_dirs_for_code(root, "ONSD-340A")
    assert find_export_dirs_for_code(root, "ONSD-340B") == []
    assert notifications and notifications[0][1] == "Deleted"

    # Re-queue A, clear library presence, fail notify → code stays pending.
    queue.mark(["ONSD-340A"])
    present.clear()

    def boom(paths, kind: str) -> None:
        raise RuntimeError("notification unavailable")

    failed = queue.drain(settings=settings, code_exists=lambda _code: False, notify=boom)
    assert "ONSD-340A" in failed.remaining
    assert failed.errors
    # Directory was removed before notify failed; code remains for retry.
    assert find_export_dirs_for_code(root, "ONSD-340A") == []

    # Retry succeeds (nothing left on disk) and clears the queue.
    done = queue.drain(settings=settings, code_exists=lambda _code: False, notify=notify)
    assert done.remaining == []
    assert queue.codes == ()


def test_remove_export_dirs_for_code_prunes_empty_studio_parent(tmp_path: Path) -> None:
    root = tmp_path / "export"
    studio = root / "Studio"
    folder = studio / "GONE-1"
    folder.mkdir(parents=True)
    write_sidecar(folder, "GONE-1", [], work_id=None)
    (folder / "GONE-1.strm").write_text("http://x\n", encoding="utf-8")
    removed = remove_export_dirs_for_code(root, "GONE-1")
    assert folder in removed or any(path == folder for path in removed)
    assert not folder.exists()
    assert not studio.exists()


def test_scanner_binds_letter_volumes_to_directory_code(tmp_path: Path) -> None:
    from shadow_mdc.db.repository import Database, Repository
    from shadow_mdc.services.scanner import Scanner

    library_root = tmp_path / "lib" / "ONSD-340"
    library_root.mkdir(parents=True)
    (library_root / "a.mp4").write_bytes(b"video-a" * 1000)
    (library_root / "B.mkv").write_bytes(b"video-b" * 1000)
    database = Database(f"sqlite:///{tmp_path / 'vol-merge.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        library = repo.create_library(
            name="vol",
            root_path=str(tmp_path / "lib"),
            recursive=True,
            organize_template="{studio}/{code}/{file}",
        )
        scanner = Scanner(repo)
        result = scanner.scan(library)
        assert result.discovered == 2
        assets = repo.list_assets()
        assert len(assets) == 2
        for asset in assets:
            hints = asset.hints or {}
            assert hints.get("code") == "ONSD-340"
            assert "directory-volume-merge" in (hints.get("alias_evidence") or [])


def test_remove_export_dirs_keeps_custom_sidecars(tmp_path: Path) -> None:
    """Code-correction cleanup deletes only generated names (miyabi idea)."""

    root = tmp_path / "export"
    folder = root / "SSIS-001"
    folder.mkdir(parents=True)
    write_sidecar(folder, "SSIS-001", [], work_id=None)
    (folder / "SSIS-001.strm").write_text("http://x\n", encoding="utf-8")
    (folder / "SSIS-001-cd2.strm").write_text("http://y\n", encoding="utf-8")
    (folder / "movie.nfo").write_text("<movie/>\n", encoding="utf-8")
    (folder / "poster.jpg").write_bytes(b"P")
    (folder / "fanart.jpg").write_bytes(b"F")
    (folder / "bonus.strm").write_text("keep\n", encoding="utf-8")
    (folder / "custom.nfo").write_text("keep\n", encoding="utf-8")
    (folder / "notes.txt").write_text("keep\n", encoding="utf-8")
    remove_export_dirs_for_code(root, "SSIS-001")
    assert folder.is_dir()
    left = sorted(path.name for path in folder.iterdir())
    assert left == ["bonus.strm", "custom.nfo", "notes.txt"]
