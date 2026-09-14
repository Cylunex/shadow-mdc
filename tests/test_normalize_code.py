"""Unit tests for javranking-style normalize_code / to_comparison_key."""

from __future__ import annotations

from shadow_mdc.normalize_code import normalize_code, to_comparison_key

_FULLWIDTH_FC2 = (
    "\uff26\uff23\uff12\uff0d\uff13\uff10\uff16\uff11\uff16\uff12\uff15"  # fullwidth FC2-3061625
)


def test_normalize_code_spaces_dashes_underscores() -> None:
    assert normalize_code("abp 123") == "ABP-123"
    assert normalize_code("ABP-123") == "ABP-123"
    assert normalize_code("abp_123") == "ABP-123"
    assert normalize_code("  abp   123  ") == "ABP-123"
    assert normalize_code("abp--123") == "ABP-123"


def test_normalize_code_nfkc_and_fc2_ppv() -> None:
    assert normalize_code(_FULLWIDTH_FC2) == "FC2-3061625"
    assert normalize_code("FC2 PPV 1234567") == "FC2-1234567"
    assert normalize_code("FC2-PPV-1234567") == "FC2-1234567"


def test_normalize_code_dotted_and_empty() -> None:
    assert normalize_code("blacked.20.01.10") == "BLACKED.20.01.10"
    assert normalize_code("") == ""
    assert normalize_code(None) == ""


def test_to_comparison_key() -> None:
    assert to_comparison_key("ABP-123") == "ABP123"
    assert to_comparison_key("abp 123") == "ABP123"
    assert to_comparison_key("abp_123") == "ABP123"
    assert to_comparison_key("abp--123") == "ABP123"
    assert to_comparison_key("ABP123") == "ABP123"
    assert to_comparison_key("FC2-3061625") == "FC23061625"
    assert to_comparison_key("FC2 PPV 3061625") == "FC23061625"
    assert to_comparison_key(_FULLWIDTH_FC2) == "FC23061625"
    assert to_comparison_key("") == ""
    assert to_comparison_key(None) == ""
