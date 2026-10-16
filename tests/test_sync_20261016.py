"""Sync 2026-10-16: atomic sidecars, narrow export cleanup, r18 dump hardening."""

from __future__ import annotations

import gzip
import os
import stat
import time
from pathlib import Path

import httpx
import pytest

from shadow_mdc.media.atomicfile import write_bytes, write_text
from shadow_mdc.services.r18_dump import import_dump_to_sqlite
from shadow_mdc.services.r18_dump_download import (
    EOF_PROBE_HEADER_TIMEOUT,
    NetworkStallWatch,
    download_latest_dump,
)
from shadow_mdc.services.strm_export import (
    cleanup_generated_export_files,
    is_generated_export_name,
    write_sidecar,
)


def test_atomicfile_preserves_mode_and_leaves_target_on_failed_replace(tmp_path: Path) -> None:
    target = tmp_path / "movie.nfo"
    target.write_text("old\n", encoding="utf-8")
    os.chmod(target, 0o640)
    write_text(target, "new\n")
    assert target.read_text(encoding="utf-8") == "new\n"
    assert stat.S_IMODE(target.stat().st_mode) == 0o640

    link = tmp_path / "alias.nfo"
    link.symlink_to(target)
    write_text(link, "via-link\n")
    assert link.is_symlink()
    assert target.read_text(encoding="utf-8") == "via-link\n"


def test_atomicfile_failed_writer_keeps_previous_bytes(tmp_path: Path) -> None:
    from shadow_mdc.media.atomicfile import write_via

    target = tmp_path / "poster.jpg"
    target.write_bytes(b"KEEP")

    def boom(temporary: Path) -> None:
        temporary.write_bytes(b"half")
        raise RuntimeError("disk full")

    with pytest.raises(RuntimeError, match="disk full"):
        write_via(target, boom)
    assert target.read_bytes() == b"KEEP"
    assert not any(tmp_path.glob(".poster.jpg.*.tmp"))


def test_generated_export_name_rules() -> None:
    assert is_generated_export_name("SSIS-001.strm", "SSIS-001")
    assert is_generated_export_name("SSIS-001-cd12.strm", "SSIS-001")
    assert is_generated_export_name("movie.nfo", "SSIS-001")
    assert is_generated_export_name("poster.JPG", "SSIS-001")
    assert not is_generated_export_name("bonus.strm", "SSIS-001")
    assert not is_generated_export_name("custom.nfo", "SSIS-001")
    assert not is_generated_export_name("SSIS-001-cd01.strm", "SSIS-001")  # leading zero
    assert not is_generated_export_name("SSIS-002.strm", "SSIS-001")


def test_cleanup_generated_keeps_custom_files(tmp_path: Path) -> None:
    folder = tmp_path / "SSIS-001"
    folder.mkdir()
    write_sidecar(folder, "SSIS-001", [], work_id=None)
    (folder / "SSIS-001.strm").write_text("x\n", encoding="utf-8")
    (folder / "bonus.strm").write_text("keep\n", encoding="utf-8")
    (folder / "notes.txt").write_text("keep\n", encoding="utf-8")
    deleted = cleanup_generated_export_files(folder, "SSIS-001")
    assert {path.name for path in deleted} >= {"SSIS-001.strm", ".shadow-strm.json"}
    assert sorted(path.name for path in folder.iterdir()) == ["bonus.strm", "notes.txt"]


def test_network_stall_watch_pauses_during_import_window() -> None:
    watch = NetworkStallWatch(limit_seconds=0.05)
    watch.beat()
    watch.pause()  # local import / EOF probe
    time.sleep(0.08)
    assert not watch.stalled()
    watch.resume()
    watch.beat()
    time.sleep(0.08)
    assert watch.stalled()


def test_eof_probe_header_timeout_constant() -> None:
    assert EOF_PROBE_HEADER_TIMEOUT == 10.0


def test_download_unknown_length_eof_probe_416(tmp_path: Path) -> None:
    """Clean EOF + unknown length → bounded Range probe confirms completeness."""

    payload = b"\x1f\x8b" + b"x" * 64
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        range_header = request.headers.get("range")
        calls.append(f"{request.method}:{request.url.path}:{range_header}")
        if request.url.path.endswith("/latest") and range_header is None:
            return httpx.Response(
                200,
                headers={"etag": '"abc"', "content-type": "application/gzip"},
                stream=httpx.ByteStream(payload),
            )
        if range_header == f"bytes={len(payload)}-":
            return httpx.Response(
                416,
                headers={"content-range": f"bytes */{len(payload)}"},
                stream=httpx.ByteStream(b""),
            )
        return httpx.Response(500, stream=httpx.ByteStream(b"nope"))

    transport = httpx.MockTransport(handler)
    probe_events: list[str] = []
    with httpx.Client(transport=transport) as client:
        result = download_latest_dump(
            tmp_path,
            client=client,
            latest_url="https://r18.test/dumps/latest",
            on_eof_probe_start=lambda: probe_events.append("start"),
            on_eof_probe_done=lambda: probe_events.append("done"),
        )
    assert result.path.is_file()
    assert result.path.read_bytes() == payload
    assert probe_events == ["start", "done"]
    assert any(item.endswith(f":bytes={len(payload)}-") for item in calls)


def test_import_cancel_rolls_back_and_deletes_temp_db(tmp_path: Path) -> None:
    dump = tmp_path / "mini.sql.gz"
    sql = """
COPY public.derived_maker (id, name_en, name_ja) FROM stdin;
1\tS1\tStudio
\\.
COPY public.derived_video (content_id, dvd_id, title_en, title_ja, comment_en, comment_ja, runtime_mins, release_date, sample_url, maker_id, label_id, series_id, jacket_full_url, jacket_thumb_url, gallery_full_first, gallery_full_last, gallery_thumb_first, gallery_thumb_last, site_id, service_code) FROM stdin;
ssis001\tSSIS-001\tEN\tJA\t\\N\t\\N\t60\t2020-01-01\t\\N\t1\t\\N\t\\N\t\\N\t\\N\t\\N\t\\N\t\\N\t\\N\t1\tdvd
\\.
"""
    with gzip.open(dump, "wt", encoding="utf-8") as handle:
        handle.write(sql)
    db = tmp_path / "r18.db"

    class Cancelled(Exception):
        pass

    ticks = {"n": 0}

    def cancel() -> None:
        ticks["n"] += 1
        if ticks["n"] >= 1:
            raise Cancelled("stop")

    with pytest.raises(Cancelled):
        import_dump_to_sqlite(dump, db, progress_every=0, cancel_check=cancel)
    assert not db.exists()
    assert not (tmp_path / "r18.db.tmp").exists()
