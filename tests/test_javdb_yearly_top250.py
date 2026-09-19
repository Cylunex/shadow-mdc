"""Offline tests for local-first JavDB yearly TOP250 export/load."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

from shadow_mdc.services.javdb_yearly_top250 import (
    export_historical_from_jinjier,
    listing_from_items,
    parse_code_and_title,
    read_yearly_list,
    sections_from_local_yearly,
    write_yearly_list,
    YearlyTop250Item,
)
from shadow_mdc.services.javdb_yearly_top250_seed import (
    merge_top250_year_sections,
    parse_code_from_jinjier_name,
)


def test_parse_code_and_title() -> None:
    code, title = parse_code_and_title("ABF-087 俺の従順ペット候補生 06 涼森れむ")
    assert code == "ABF-087"
    assert "涼森" in title
    parsed = parse_code_from_jinjier_name("LAFBD-41  無碼 ラフォーレ")
    assert parsed.code.startswith("LAFBD")


def test_export_and_freeze(tmp_path: Path) -> None:
    db = tmp_path / "jinjier.sqlite3"
    conn = sqlite3.connect(str(db))
    conn.execute(
        "CREATE TABLE ranks (number TEXT, name TEXT, date TEXT, icon_url TEXT, note TEXT)"
    )
    for i in range(1, 4):
        conn.execute(
            "INSERT INTO ranks VALUES (?, ?, ?, ?, ?)",
            (str(i), f"TEST-{i:03d} Title {i}", f"2020-01-{i:02d}", f"http://x/{i}.jpg", "JavDB 2020 TOP250"),
        )
    # duplicate code at worse rank should be dropped
    conn.execute(
        "INSERT INTO ranks VALUES (?, ?, ?, ?, ?)",
        ("99", "TEST-001 Dup", "2020-02-01", None, "JavDB 2020 TOP250"),
    )
    conn.commit()
    conn.close()

    data_dir = tmp_path / "data"
    written = export_historical_from_jinjier(
        db, data_dir, years=[2020], today=date(2026, 9, 19)
    )
    assert len(written) == 1
    assert written[0].year == 2020
    assert len(written[0].items) == 4
    assert written[0].frozen is True

    path = data_dir / "javranking" / "yearly" / "javdb-top250-2020.json"
    assert path.is_file()
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["items"][0]["code"] == "TEST-001"
    assert payload["items"][0]["rank"] == 1

    # second export without force keeps frozen file
    again = export_historical_from_jinjier(
        db, data_dir, years=[2020], today=date(2026, 9, 19)
    )
    assert again[0].revision == written[0].revision

    sections = sections_from_local_yearly(data_dir, today=date(2026, 9, 19))
    assert any(year == 2020 and count == 4 for _, year, _, count, _ in sections)


def test_merge_prefers_disk_and_fills_range() -> None:
    merged = merge_top250_year_sections(
        [("javdb-top250-2025", 2025, "idx")],
        [("javdb-top250-2020", 2020, "disk", 250)],
        year_start=2019,
        year_end=2021,
    )
    slugs = {row[0]: row for row in merged}
    assert slugs["javdb-top250-2020"][3] == 250
    assert "javdb-top250-2019" in slugs
    assert "javdb-top250-2021" in slugs
