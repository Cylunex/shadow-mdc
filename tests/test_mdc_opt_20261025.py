from __future__ import annotations

import asyncio
import gzip
import sqlite3
import threading
import time
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from shadow_mdc.providers.r18_dump import R18DumpProvider, R18DumpWorker
from shadow_mdc.services.r18_dump import (
    R18DumpStore,
    R18DumpValidationError,
    _dvd_keys,
    import_dump_to_sqlite,
)
from shadow_mdc.services.rescrape_quota import RescrapeQuota

_VIDEO_COLS = (
    "content_id, dvd_id, title_en, title_ja, comment_en, comment_ja, runtime_mins, release_date, "
    "sample_url, maker_id, label_id, series_id, jacket_full_url, jacket_thumb_url, "
    "gallery_full_first, gallery_full_last, gallery_thumb_first, gallery_thumb_last, site_id, service_code"
)


def _video(cid: str, dvd: str, title: str, service: str) -> str:
    cells = [cid, dvd, title, title] + ["\\N"] * 14 + ["1", service]
    return "\t".join(cells)


def _dump(path: Path, rows: list[str]) -> Path:
    body = f"COPY public.derived_video ({_VIDEO_COLS}) FROM stdin;\n" + "\n".join(rows) + "\n\\.\n"
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(body)
    return path


def test_same_content_id_two_services_is_ambiguous_miss(tmp_path: Path) -> None:
    dump = _dump(
        tmp_path / "d.sql.gz",
        [_video("ssis00123", "SSIS-123", "A", "digital"), _video("ssis00123", "SSIS-123", "B", "mono")],
    )
    db = tmp_path / "r18.db"
    import_dump_to_sqlite(dump, db, progress_every=0)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT COUNT(*) FROM videos").fetchone()[0] == 2  # both kept
    with R18DumpStore(db) as store:
        match = store.match_video("SSIS-123")
        assert match.row is None
        assert set(match.ambiguous) == {"ssis00123/digital", "ssis00123/mono"}
        assert store.build_record("SSIS-123") is None


def test_unique_hit_and_case_preserved_with_fold_alias(tmp_path: Path) -> None:
    dump = _dump(tmp_path / "d.sql.gz", [_video("H_123ABC00042", "\\N", "Upper", "digital")])
    db = tmp_path / "r18.db"
    import_dump_to_sqlite(dump, db, progress_every=0)
    with R18DumpStore(db) as store:
        row = store._unique("content_id_fold", "h_123abc00042").row
        assert row is not None and row["content_id"] == "H_123ABC00042"  # original case kept


def test_dvd_id_norm_is_nfkc(tmp_path: Path) -> None:
    assert _dvd_keys("ＳＳＩＳ－１２３") == _dvd_keys("SSIS-123")
    dump = _dump(tmp_path / "d.sql.gz", [_video("ssis00123", "ＳＳＩＳ－１２３", "T", "digital")])
    db = tmp_path / "r18.db"
    import_dump_to_sqlite(dump, db, progress_every=0)
    with R18DumpStore(db) as store:
        assert store.build_record("SSIS-123") is not None
        assert store.build_record("ＳＳＩＳ－１２３") is not None


def test_legacy_v1_db_still_readable(tmp_path: Path) -> None:
    db = tmp_path / "old.db"
    with sqlite3.connect(db) as conn:
        conn.executescript(
            "CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);"
            "CREATE TABLE videos(content_id TEXT PRIMARY KEY, dvd_id TEXT, dvd_id_norm TEXT, dvd_id_key TEXT,"
            " title_ja TEXT, service_code TEXT);"
            "INSERT INTO videos VALUES('ABC00001', NULL, NULL, NULL, 't', 'digital');"
        )
    with R18DumpStore(db) as store:
        assert store._has_fold_column() is False
        assert store._unique("lower(content_id)", "abc00001").row is not None


def test_validation_failure_keeps_old_dump(tmp_path: Path) -> None:
    db = tmp_path / "r18.db"
    rows = [_video(f"abc{i:05d}", f"ABC-{i:03d}", "t", "digital") for i in range(1, 11)]
    import_dump_to_sqlite(_dump(tmp_path / "full.sql.gz", rows), db, progress_every=0)
    before = db.read_bytes()
    with pytest.raises(R18DumpValidationError):
        import_dump_to_sqlite(_dump(tmp_path / "part.sql.gz", rows[:2]), db, progress_every=0)
    assert db.read_bytes() == before
    assert not db.with_suffix(".db.tmp").exists()
    with pytest.raises(R18DumpValidationError):
        import_dump_to_sqlite(_dump(tmp_path / "empty.sql.gz", []), tmp_path / "new.db", progress_every=0)
    assert not (tmp_path / "new.db").exists()


def test_rescrape_quota_daily_reset_and_persist(tmp_path: Path) -> None:
    tz = ZoneInfo("Asia/Shanghai")
    clock = [datetime(2026, 10, 25, 23, 0, tzinfo=tz)]
    path = tmp_path / "q.json"
    quota = RescrapeQuota(2, path=path, now=lambda: clock[0])
    assert quota.consume(["javdb", "fanza"]) == (["javdb", "fanza"], [])
    assert quota.consume(["javdb"]) == (["javdb"], [])
    assert quota.consume(["javdb", "fanza"]) == (["fanza"], ["javdb"])
    reloaded = RescrapeQuota(2, path=path, now=lambda: clock[0])
    assert reloaded.remaining("javdb") == 0
    assert reloaded.seconds_until_reset() == 3600
    clock[0] += timedelta(hours=2)
    assert reloaded.remaining("javdb") == 2


def test_dump_worker_stop_joins_bounded() -> None:
    worker = R18DumpWorker()
    assert worker.submit(lambda: 41 + 1).result(timeout=2) == 42
    assert worker.stop(timeout=2) is True
    assert not worker.alive
    assert worker.submit(lambda: 1).cancelled()

    gate = threading.Event()
    slow = R18DumpWorker()
    slow.submit(lambda: gate.wait(5))
    started = time.monotonic()
    assert slow.stop(timeout=0.2) is False  # bounded: does not hang shutdown
    assert time.monotonic() - started < 2
    gate.set()


def test_provider_shutdown_stops_worker(tmp_path: Path) -> None:
    db = tmp_path / "r18.db"
    import_dump_to_sqlite(
        _dump(tmp_path / "d.sql.gz", [_video("ssis00123", "SSIS-123", "T", "digital")]), db, progress_every=0
    )
    from shadow_mdc.domain import IdentityHints
    from shadow_mdc.enums import QueryMode

    provider = R18DumpProvider(db_path=db)
    hints = IdentityHints(term="SSIS-123", mode=QueryMode.CODE, code="SSIS-123")
    assert len(asyncio.run(provider.search(hints))) == 1
    worker = provider._worker
    assert worker is not None and worker.alive
    assert provider.shutdown(timeout=2) is True
    assert not worker.alive
    assert asyncio.run(provider.search(hints)) == []


def test_forced_rescrape_quota_429_and_all_locked_409(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from dataclasses import replace

    from fastapi.testclient import TestClient

    from shadow_mdc.api import app
    from shadow_mdc.db.repository import LOCKABLE_WORK_FIELDS
    from shadow_mdc.domain import ProviderRecord
    from shadow_mdc.enums import ContentFamily
    from shadow_mdc.providers.base import ProviderRegistry
    from tests.test_api import LookupProvider

    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'q.db'}")
    monkeypatch.setenv("SHADOW_MDC_RESCRAPE_DAILY_QUOTA", "1")
    record = ProviderRecord(
        provider="fixture", external_id="sone00118", code="SONE-118", title="T", family=ContentFamily.JAV
    )
    with TestClient(app) as client:
        app.state.runtime = replace(app.state.runtime, providers=ProviderRegistry([LookupProvider(record)]))
        work_id = client.post("/api/works/lookup", json={"code": "SONE-118"}).json()["work"]["id"]
        assert client.post(f"/api/works/{work_id}/refresh").status_code == 200
        limited = client.post(f"/api/works/{work_id}/refresh")
        assert limited.status_code == 429
        assert int(limited.headers["Retry-After"]) > 0
        client.put(f"/api/works/{work_id}/locks", json={"locks": sorted(LOCKABLE_WORK_FIELDS)})
        assert client.post(f"/api/works/{work_id}/refresh").status_code == 409
