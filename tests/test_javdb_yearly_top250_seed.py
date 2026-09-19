"""Tests for jinjier name parsing and yearly TOP250 list loading."""

from __future__ import annotations

import sqlite3
from pathlib import Path

from shadow_mdc.services.javdb_yearly_top250_seed import (
    discover_yearly_top250_on_disk,
    load_yearly_top250_from_sqlite,
    merge_top250_year_sections,
    parse_code_from_jinjier_name,
    seed_javdb_yearly_top250,
    write_yearly_list_files,
    yearly_slug,
)
from shadow_mdc.services.javranking_client import CuratedList


def test_parse_code_from_jinjier_name_standard_and_uncensored() -> None:
    assert parse_code_from_jinjier_name("DDK-023 おかんと一緒にラブホテル").code == "DDK-023"
    assert parse_code_from_jinjier_name("n0299  無碼 罠!美形").code == "N0299"
    assert parse_code_from_jinjier_name("091208_424  無碼 突撃").code == "091208-424"
    assert parse_code_from_jinjier_name("MKBD-S03  無碼 KIRARI").code == "MKBD-S03"
    assert parse_code_from_jinjier_name("ABF-087 俺の従順ペット").title.startswith("俺の従順")
    empty = parse_code_from_jinjier_name("")
    assert empty.code == "" and empty.title == ""


def _write_mini_sqlite(path: Path) -> None:
    conn = sqlite3.connect(str(path))
    conn.execute(
        "CREATE TABLE ranks (number TEXT, name TEXT, date TEXT, icon_url TEXT, note TEXT)"
    )
    rows = [
        ("1", "DDK-023 title a", "2008-10-19", "https://example/a.jpg", "JavDB 2008 TOP250"),
        ("2", "n0299  無碼 title b", "2008-02-08", "https://example/b.jpg", "JavDB 2008 TOP250"),
        ("3", "n0299  無碼 title b", "2008-02-08", "https://example/b.jpg", "JavDB 2008 TOP250"),
        ("1", "SOE-121 title c", "2009-01-01", None, "JavDB 2009 TOP250"),
        ("1", "IPX-811 overall", "2020-01-01", None, "JavDB TOP250"),
    ]
    conn.executemany("INSERT INTO ranks VALUES (?,?,?,?,?)", rows)
    conn.commit()
    conn.close()


def test_load_yearly_top250_dedupes_and_filters(tmp_path: Path) -> None:
    db = tmp_path / "jinjier.sqlite3"
    _write_mini_sqlite(db)
    entries = load_yearly_top250_from_sqlite(db, years=[2008, 2009])
    by_year = {}
    for entry in entries:
        by_year.setdefault(entry.year, []).append(entry)
    assert sorted(by_year) == [2008, 2009]
    assert len(by_year[2008]) == 2  # n0299 deduped
    assert by_year[2008][0].code == "DDK-023"
    assert by_year[2008][1].code == "N0299"
    assert by_year[2008][1].position == 2  # renumbered after dedupe
    assert by_year[2009][0].code == "SOE-121"


def test_write_yearly_lists_and_discover(tmp_path: Path) -> None:
    db = tmp_path / "jinjier.sqlite3"
    _write_mini_sqlite(db)
    cache_dir = tmp_path / "javranking"
    entries = load_yearly_top250_from_sqlite(db, years=[2008])
    written = write_yearly_list_files(cache_dir, entries)
    assert written == ["javdb-top250-2008"]
    path = cache_dir / "list-javdb-top250-2008.json"
    assert path.is_file()
    curated = CuratedList.model_validate_json(path.read_text(encoding="utf-8"))
    assert curated.kind == "videos"
    assert curated.source_format == "jinjier-sqlite"
    assert len(curated.videos) == 2
    assert curated.videos[0].cover_url == "https://example/a.jpg"

    found = discover_yearly_top250_on_disk(cache_dir)
    assert found == [("javdb-top250-2008", 2008, "JavDB 2008 TOP250", 2)]

    merged = merge_top250_year_sections(
        [("javdb-top250-2023", 2023, "JAVDB TOP250 2023")],
        found,
        year_start=2008,
        year_end=2009,
    )
    by_slug = {item[0]: item for item in merged}
    assert by_slug["javdb-top250-2008"][3] == 2
    assert by_slug["javdb-top250-2023"][1] == 2023
    assert by_slug["javdb-top250-2009"][3] == 0  # placeholder chip


def test_seed_lists_only_pipeline(tmp_path: Path) -> None:
    db = tmp_path / "jinjier.sqlite3"
    _write_mini_sqlite(db)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    result = seed_javdb_yearly_top250(
        sqlite_path=db,
        data_dir=data_dir,
        repo=None,
        years=[2008, 2009],
        write_lists=True,
        seed_catalog=False,
    )
    assert list(result.years) == [2008, 2009]
    assert yearly_slug(2008) in result.lists_written
    assert (data_dir / "javranking" / "list-javdb-top250-2009.json").is_file()
    assert result.entry_counts["javdb-top250-2008"] == 2


def test_javdb_yearly_module_parse_and_export(tmp_path: Path) -> None:
    from shadow_mdc.services.javdb_yearly_top250 import (
        export_historical_from_jinjier,
        list_local_yearly_years,
        parse_code_and_title,
        read_yearly_list,
    )

    code, title = parse_code_and_title("091208_424  無碼 突撃")
    assert code == "091208-424"
    assert "突撃" in title

    db = tmp_path / "jinjier.sqlite3"
    _write_mini_sqlite(db)
    written = export_historical_from_jinjier(db, tmp_path, years=[2008, 2009], force=True)
    assert {item.year for item in written} == {2008, 2009}
    assert list_local_yearly_years(tmp_path) == [2008, 2009]
    listing = read_yearly_list(tmp_path, 2008)
    assert listing is not None
    codes = [item.code for item in listing.items]
    assert "DDK-023" in codes
    assert any(code.startswith("N0299") or code == "N0299" for code in codes)
    assert listing.frozen is True
