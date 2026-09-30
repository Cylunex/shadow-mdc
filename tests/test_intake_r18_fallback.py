"""Intake r18-dump fallback + JavDB rankings URL (no network)."""

from __future__ import annotations

import asyncio
import gzip
from pathlib import Path

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.services.discover import DiscoverService, javdb_list_url, parse_javdb_list
from shadow_mdc.services.r18_dump import R18DumpStore, import_dump_to_sqlite

FIXTURES = Path(__file__).parent / "fixtures"

_VIDEO_COLS = (
    "content_id, dvd_id, title_en, title_ja, comment_en, comment_ja, runtime_mins, release_date, "
    "sample_url, maker_id, label_id, series_id, jacket_full_url, jacket_thumb_url, gallery_full_first, "
    "gallery_full_last, gallery_thumb_first, gallery_thumb_last, site_id, service_code"
)


def _video(content_id: str, dvd_id: str, title: str, date: str) -> str:
    cover = f"digital/video/{content_id}/{content_id}"
    values = [
        content_id,
        dvd_id,
        "\\N",
        title,
        "\\N",
        "\\N",
        "120",
        date,
        "\\N",
        "1",
        "\\N",
        "\\N",
        f"{cover}pl",
        f"{cover}ps",
        "\\N",
        "\\N",
        "\\N",
        "\\N",
        "1",
        "digital",
    ]
    return "\t".join(values)


def _build_dump(tmp_path: Path) -> Path:
    rows = [
        _video("118abf304", "ABF-304", "旧作", "2026-01-16"),
        _video("118abf305", "ABF-305", "旧作2", "2026-01-16"),
        _video("118abf387", "\\N", "新作タイトル", "2026-10-02"),
        _video("118tktabf387", "\\N", "特典版", "2026-10-02"),
        _video("1start00634", "START-634", "スタート", "2026-09-22"),
    ]
    sql = (
        "COPY public.derived_maker (id, name_en, name_ja) FROM stdin;\n1\tPrestige\tプレステージ\n\\.\n"
        "COPY public.derived_actress (id, name_romaji, image_url, name_kanji, name_kana) FROM stdin;\n"
        "10\tNene\t\\N\t吉高寧々\t\\N\n\\.\n"
        f"COPY public.derived_video ({_VIDEO_COLS}) FROM stdin;\n" + "\n".join(rows) + "\n\\.\n"
        "COPY public.derived_video_actress (content_id, actress_id, ordinality, release_date) FROM stdin;\n"
        "118abf387\t10\t1\t2026-10-02\n\\.\n"
    )
    dump = tmp_path / "mini.sql.gz"
    with gzip.open(dump, "wt", encoding="utf-8") as handle:
        handle.write(sql)
    db = tmp_path / "r18.db"
    import_dump_to_sqlite(dump, db, progress_every=0)
    return db


def test_sibling_prefix_inference_finds_null_dvd_id_rows(tmp_path: Path) -> None:
    db = _build_dump(tmp_path)
    with R18DumpStore(db) as store:
        record = store.build_record("ABF-387")
        assert record is not None
        assert record.external_id == "118abf387"
        assert record.code == "ABF-387"
        assert record.title == "新作タイトル"
        assert record.actors == ("吉高寧々",)
        assert record.studio == "プレステージ"
        assert store.build_record("ABF-999") is None


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    return database


def test_seed_falls_back_to_r18_dump_when_online_empty(tmp_path: Path) -> None:
    db = _build_dump(tmp_path)
    discover = DiscoverService(ProviderRegistry([]), None, None, r18_dump_path=db)
    database = _database(tmp_path)
    with database.session() as session:
        repo = Repository(session)
        result = asyncio.run(discover.seed(repo, provider="fanza", code="ABF-387"))
        assert result.created is True
        assert result.fallback == "r18dump"
        work = repo.get_work(result.work_id)
        assert work is not None
        assert work.primary_code == "ABF-387"
        assert work.release_date is not None and work.release_date.isoformat() == "2026-10-02"
        assert work.runtime_seconds == 7200
        assert any("pics.dmm.co.jp" in str(item.get("url")) for item in work.artwork)
        # Second call reuses the existing work instead of creating a duplicate.
        again = asyncio.run(discover.seed(repo, provider="fanza", code="ABF-387"))
        assert again.created is False
        assert again.work_id == result.work_id
    discover.close()


def test_seed_without_dump_degrades_to_lookup_error(tmp_path: Path) -> None:
    discover = DiscoverService(ProviderRegistry([]), None, None, r18_dump_path=tmp_path / "missing.db")
    assert discover.r18_fallback_available is False
    database = _database(tmp_path)
    with database.session() as session, pytest.raises(LookupError):
        asyncio.run(discover.seed(Repository(session), provider="fanza", code="START-634"))


def test_seed_fallback_can_be_disabled(tmp_path: Path) -> None:
    db = _build_dump(tmp_path)
    discover = DiscoverService(ProviderRegistry([]), None, None, r18_dump_path=db)
    database = _database(tmp_path)
    with database.session() as session, pytest.raises(LookupError):
        asyncio.run(
            discover.seed(Repository(session), provider="fanza", code="START-634", allow_r18_fallback=False)
        )
    discover.close()


def test_javdb_rankings_url_uses_current_format() -> None:
    assert javdb_list_url("https://javdb.com", "rankings_daily", 1) == (
        "https://javdb.com/rankings/movies?p=daily&t=censored"
    )
    assert javdb_list_url("https://javdb.com/", "rankings_monthly", 3) == (
        "https://javdb.com/rankings/movies?p=monthly&t=censored"
    )
    assert javdb_list_url("https://javdb.com", "latest", 2) == "https://javdb.com/?page=2"


def test_parse_javdb_rankings_fixture() -> None:
    html = (FIXTURES / "javdb_rankings_daily.html").read_text(encoding="utf-8")
    items = parse_javdb_list(html, "https://javdb.com")
    assert [item.code for item in items] == ["DLDSS-553", "START-640", "BBAN-600"]
    assert items[0].external_id == "MbB5gX"
    assert items[0].source_url == "https://javdb.com/v/MbB5gX"
    assert parse_javdb_list("<html><body>404</body></html>", "https://javdb.com") == []
