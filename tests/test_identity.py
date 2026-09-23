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
        ("HEYDOUGA-4037-123.mp4", "heydouga-4037-123", ContentFamily.JAV),
        ("GETCHU_123456.mp4", "GETCHU-123456", ContentFamily.JAV),
        ("GYUTTO-654321.mkv", "GYUTTO-654321", ContentFamily.JAV),
        ("IBW-123z.mp4", "IBW-123Z", ContentFamily.JAV),
        ("T28-557.mp4", "T28-557", ContentFamily.JAV),
        ("1pondo-012345_678.mp4", "1PONDO-012345-678", ContentFamily.JAV),
        ("010115-001-carib.mp4", "CARIB-010115-001", ContentFamily.JAV),
        ("carib-070116_197.mp4", "CARIB-070116-197", ContentFamily.JAV),
        ("070116-197-1pon.mp4", "1PONDO-070116-197", ContentFamily.JAV),
        ("pacopacomama-010203_04.mp4", "PACOPACOMAMA-010203-04", ContentFamily.JAV),
        ("caribbeancom 070116-197.mp4", "070116-197", ContentFamily.JAV),
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
