from pathlib import Path

import pytest

from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.identity import IdentityAliasRules, build_identity_hints, clean_stem, extract_code


@pytest.mark.parametrize(
    ("value", "expected_code", "expected_family"),
    [
        ("SSIS-123-C.mp4", "SSIS-123", ContentFamily.JAV),
        ("SONE-118A.mp4", "SONE-118", ContentFamily.JAV),
        ("SONE-118-partB.mp4", "SONE-118", ContentFamily.JAV),
        ("FC2 PPV 1234567.mkv", "FC2-1234567", ContentFamily.JAV),
        ("HEYZO_1234_1080p.mp4", "HEYZO-1234", ContentFamily.JAV),
        ("1pondo-012345_678.mp4", "1PONDO-012345-678", ContentFamily.JAV),
        ("MDSR-001.mp4", "MDSR-001", ContentFamily.CHINESE),
        ("StudioName.24.01.02.Performer.mp4", "studioname.24.01.02", ContentFamily.WESTERN),
        ("H264-1080.mp4", None, ContentFamily.UNKNOWN),
        ("没有稳定编号的作品.mkv", None, ContentFamily.UNKNOWN),
    ],
)
def test_extract_code_table(
    value: str,
    expected_code: str | None,
    expected_family: ContentFamily,
) -> None:
    assert extract_code(clean_stem(value)) == (expected_code, expected_family)


def test_clean_stem_removes_release_noise_without_losing_title() -> None:
    assert clean_stem("[example.com] 标题.中文字幕.2160p.mkv") == "标题"


def test_code_is_primary_query_even_when_file_has_fingerprint(tmp_path: Path) -> None:
    hints = build_identity_hints(tmp_path / "SSIS-123.mkv", fingerprints={"oshash": "0123456789abcdef"})

    assert hints.mode is QueryMode.CODE
    assert hints.term == "SSIS-123"
    assert hints.fingerprints == {"oshash": "0123456789abcdef"}


def test_url_and_external_id_take_precedence() -> None:
    by_url = build_identity_hints("title.mkv", source_url="https://example.test/work/1")
    by_id = build_identity_hints("title.mkv", external_ids={"catalog": "42"})

    assert by_url.mode is QueryMode.URL
    assert by_id.mode is QueryMode.EXTERNAL_ID


def test_ascii_alias_requires_token_boundaries() -> None:
    rules = IdentityAliasRules(studios={"xb": "杏吧传媒"})

    matched = build_identity_hints("xb-title.mp4", alias_rules=rules)
    unrelated = build_identity_hints("xbox-title.mp4", alias_rules=rules)

    assert matched.studio == "杏吧传媒"
    assert unrelated.studio is None


def test_alias_rules_reject_blank_entries() -> None:
    with pytest.raises(ValueError, match="must not be blank"):
        IdentityAliasRules(series={"": "系列"})
