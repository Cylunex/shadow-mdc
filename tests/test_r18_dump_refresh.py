from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import select

from shadow_mdc.config import Settings
from shadow_mdc.db.models import Work
from shadow_mdc.db.repository import Database
from shadow_mdc.enums import ContentFamily
from shadow_mdc.services.r18_dump import import_dump_to_sqlite
from shadow_mdc.services.r18_dump_download import R18DumpDownloadResult
from shadow_mdc.services.r18_dump_refresh import (
    DEFAULT_REFRESH_CRON,
    DEFAULT_REFRESH_TIMEZONE,
    JavCatalogGapCounts,
    assert_enrich_did_not_grow_catalog,
    count_jav_catalog_gaps_from_database,
    enrich_existing_jav_from_dump,
    refresh_r18_dump,
    resolve_proxy,
)
from tests.test_r18_dump import _write_mini_dump


def test_settings_r18_dump_refresh_defaults() -> None:
    fields = Settings.model_fields
    assert fields["r18_dump_refresh_enabled"].default is False
    assert fields["r18_dump_refresh_cron"].default == DEFAULT_REFRESH_CRON == "1 10 * * 3"
    assert fields["r18_dump_refresh_timezone"].default == DEFAULT_REFRESH_TIMEZONE == "Asia/Shanghai"
    assert fields["r18_dump_refresh_service"].default == "shadow-mdc"
    assert fields["r18_dump_refresh_health_url"].default == "http://127.0.0.1:8700/api/health"


def test_resolve_proxy_prefers_explicit_then_settings() -> None:
    settings = Settings.model_construct(proxy_url="http://settings:7890")
    assert resolve_proxy(settings, "http://cli:1") == "http://cli:1"
    assert resolve_proxy(settings, None) == "http://settings:7890"
    assert resolve_proxy(Settings.model_construct(proxy_url=None), None) is None
    assert resolve_proxy(Settings.model_construct(proxy_url="  "), "  ") is None


def test_count_jav_catalog_gaps(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    with database.session() as session:
        session.add_all(
            [
                Work(
                    title="Complete",
                    primary_code="SSIS-001",
                    family=ContentFamily.JAV.value,
                    actors=["A"],
                    studio="Maker",
                    release_date=date(2021, 1, 1),
                    runtime_seconds=6000,
                ),
                Work(
                    title="Gaps",
                    primary_code="SSIS-002",
                    family=ContentFamily.JAV.value,
                    actors=[],
                    studio=None,
                    release_date=None,
                    runtime_seconds=None,
                ),
                Work(
                    title="Non-JAV",
                    primary_code=None,
                    family=ContentFamily.CHINESE.value,
                    actors=[],
                ),
            ]
        )
        session.commit()
    counts = count_jav_catalog_gaps_from_database(database)
    assert counts == JavCatalogGapCounts(
        jav_total=2,
        missing_actress=1,
        missing_maker=1,
        missing_release_date=1,
        missing_duration=1,
    )


def test_assert_enrich_did_not_grow_catalog() -> None:
    before = JavCatalogGapCounts(10, 1, 1, 1, 1)
    assert_enrich_did_not_grow_catalog(before, JavCatalogGapCounts(10, 0, 0, 0, 0))
    assert_enrich_did_not_grow_catalog(before, JavCatalogGapCounts(9, 0, 0, 0, 0))
    with pytest.raises(RuntimeError, match="jav_total"):
        assert_enrich_did_not_grow_catalog(before, JavCatalogGapCounts(11, 0, 0, 0, 0))


def test_refresh_skips_import_when_unchanged(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    settings = Settings.model_construct(data_dir=data_dir, r18_dump_db=None, proxy_url=None)
    settings.ensure_directories()
    dump = data_dir / "r18-dumps" / "db_dump_2026-10-14.sql.gz"
    sqlite_path = data_dir / "r18-dumps" / "r18_dump.db"
    _write_mini_dump(dump)
    import_dump_to_sqlite(dump, sqlite_path, progress_every=0)
    fake = R18DumpDownloadResult(
        path=dump,
        final_url="https://r18.dev/dumps/db_dump_2026-10-14.sql.gz",
        source_date="2026-10-14",
        bytes_downloaded=0,
        resumed=False,
        unchanged=True,
    )
    with (
        patch(
            "shadow_mdc.services.r18_dump_refresh.download_latest_dump",
            return_value=fake,
        ),
        patch("shadow_mdc.services.r18_dump_refresh.httpx.Client") as client_cls,
    ):
        client_cls.return_value.__enter__.return_value = MagicMock()
        with patch("shadow_mdc.services.r18_dump_refresh.import_dump_to_sqlite") as import_mock:
            outcome = refresh_r18_dump(settings, proxy=None)
    assert outcome.download.unchanged is True
    assert outcome.imported is False
    import_mock.assert_not_called()


def test_enrich_update_only_does_not_create_works(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    db_path = data_dir / "shadow-mdc.db"
    dump_db = data_dir / "r18-dumps" / "r18_dump.db"
    settings = Settings.model_construct(
        data_dir=data_dir,
        database_url=f"sqlite:///{db_path}",
        r18_dump_db=dump_db,
        proxy_url=None,
    )
    settings.ensure_directories()
    dump = data_dir / "r18-dumps" / "mini.sql.gz"
    _write_mini_dump(dump)
    import_dump_to_sqlite(dump, dump_db, progress_every=0)

    database = Database(settings.database_url)
    database.initialize()
    with database.session() as session:
        session.add(
            Work(
                title="Existing gap",
                primary_code="SSIS-123",
                family=ContentFamily.JAV.value,
                actors=[],
                studio=None,
            )
        )
        session.commit()
    before = count_jav_catalog_gaps_from_database(database)
    outcome = enrich_existing_jav_from_dump(settings)
    after = count_jav_catalog_gaps_from_database(database)
    assert outcome.matched >= 1
    assert after.jav_total == before.jav_total == 1
    assert_enrich_did_not_grow_catalog(before, after)
    with database.session() as session:
        work = session.scalars(select(Work)).one()
        assert work.studio or work.actors


def test_import_script_stall_unbound_without_refresh(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Regression: importing an existing --dump must not reference unbound stall."""

    dump = tmp_path / "mini.sql.gz"
    _write_mini_dump(dump)
    sqlite_path = tmp_path / "out.db"
    monkeypatch.setattr(
        "sys.argv",
        [
            "import_r18_dump.py",
            "--dump",
            str(dump),
            "--sqlite",
            str(sqlite_path),
            "--data-dir",
            str(tmp_path / "data"),
        ],
    )
    path = Path(__file__).resolve().parents[1] / "scripts" / "import_r18_dump.py"
    spec = importlib.util.spec_from_file_location("import_r18_dump_under_test", path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.main() == 0
    assert sqlite_path.is_file()
