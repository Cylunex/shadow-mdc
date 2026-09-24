from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from shadow_mdc.dmm_ids import content_id_candidates
from shadow_mdc.enums import ContentFamily
from shadow_mdc.identity import extract_code
from shadow_mdc.providers.r18_dump import R18DumpProvider
from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import QueryMode
from shadow_mdc.services.r18_dump import (
    PROVIDER_ID,
    R18DumpStore,
    absolute_dmm_image,
    expand_gallery,
    import_dump_to_sqlite,
)


def test_content_id_candidates_pads_digits() -> None:
    values = content_id_candidates("SSIS-123")
    assert "ssis00123" in values
    assert "1ssis00123" in values


def test_expand_gallery_and_absolute_image() -> None:
    assert expand_gallery("xjp-1", "xjp-3") == ["xjp-1", "xjp-2", "xjp-3"]
    assert absolute_dmm_image("digital/video/a/apl") == (
        "https://pics.dmm.co.jp/digital/video/a/apl.jpg"
    )


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("H0930-ORI1234.mp4", "H0930-ORI1234"),
        ("H4610-APP042.mp4", "H4610-APP042"),
        ("TH101-000-12345.mp4", "TH101-000-12345"),
        ("KIN8-1234.mp4", "KIN8-1234"),
        ("XXX-AV-12345.mp4", "XXX-AV-12345"),
        ("S2MBD-001.mp4", "S2MBD-001"),
    ],
)
def test_mdcz_specialty_codes(raw: str, expected: str) -> None:
    code, family = extract_code(raw)
    assert family is ContentFamily.JAV
    assert code == expected


def _write_mini_dump(path: Path) -> None:
    sql = """
COPY public.derived_maker (id, name_en, name_ja) FROM stdin;
1	S1	エスワン
\\.
COPY public.derived_actress (id, name_romaji, image_url, name_kanji, name_kana) FROM stdin;
10	Yua Mikami	mikami\t三上悠亜\tみかみゆあ
\\.
COPY public.derived_video (content_id, dvd_id, title_en, title_ja, comment_en, comment_ja, runtime_mins, release_date, sample_url, maker_id, label_id, series_id, jacket_full_url, jacket_thumb_url, gallery_full_first, gallery_full_last, gallery_thumb_first, gallery_thumb_last, site_id, service_code) FROM stdin;
ssis00123	SSIS-123	EN Title	日本語タイトル	\\N	プロット	120	2021-05-01	\\N	1	\\N	\\N	digital/video/ssis00123/ssis00123pl	digital/video/ssis00123/ssis00123ps	digital/video/ssis00123/ssis00123jp-1	digital/video/ssis00123/ssis00123jp-2	\\N	\\N	1	dvd
\\.
COPY public.derived_video_actress (content_id, actress_id, ordinality, release_date) FROM stdin;
ssis00123	10	1	2021-05-01
\\.
"""
    with gzip.open(path, "wt", encoding="utf-8") as handle:
        handle.write(sql)


def test_import_and_provider_lookup(tmp_path: Path) -> None:
    dump = tmp_path / "mini.sql.gz"
    db = tmp_path / "r18.db"
    _write_mini_dump(dump)
    stats = import_dump_to_sqlite(dump, db, progress_every=0)
    assert stats.videos == 1
    assert stats.actresses == 1

    with R18DumpStore(db) as store:
        record = store.build_record("SSIS-123")
        assert record is not None
        assert record.provider == PROVIDER_ID
        assert record.title == "日本語タイトル"
        assert record.actors == ("三上悠亜",)
        assert record.studio == "エスワン"
        assert record.runtime_seconds == 7200
        assert any(item.kind == "fanart" for item in record.artwork)
        assert ("Yua Mikami", "三上悠亜") in list(store.iter_actress_alias_pairs())

    import asyncio

    provider = R18DumpProvider(db_path=db)
    hints = IdentityHints(term="SSIS-123", mode=QueryMode.CODE, code="SSIS-123")
    records = asyncio.run(provider.search(hints))
    assert len(records) == 1
    assert records[0].code == "SSIS-123"
    provider.close()
