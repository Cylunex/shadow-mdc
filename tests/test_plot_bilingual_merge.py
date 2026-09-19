from shadow_mdc.db.repository import _looks_japanese, _merge_plot_fields
from shadow_mdc.db.models import Work


def test_looks_japanese_detects_kana() -> None:
    assert _looks_japanese("禁欲の果て")
    assert not _looks_japanese("禁欲之后的中文简介")


def test_merge_preserves_japanese_as_original_when_chinese_arrives() -> None:
    work = Work(title="t", family="jav", category="japan", plot="禁欲の果て、汗と絶頂")
    sources: dict[str, str] = {"plot": "r18dev"}
    _merge_plot_fields(
        work,
        "禁欲之后的中文简介",
        provider="jav321",
        sources=sources,
        overwrite=True,
    )
    assert work.plot == "禁欲之后的中文简介"
    assert work.original_plot == "禁欲の果て、汗と絶頂"
    assert sources["plot"] == "jav321"
    assert sources["original_plot"] == "r18dev"


def test_merge_stores_japanese_incoming_as_original_when_chinese_exists() -> None:
    work = Work(title="t", family="jav", category="japan", plot="中文简介已存在")
    sources: dict[str, str] = {"plot": "jav321"}
    _merge_plot_fields(
        work,
        "日本語のあらすじです",
        provider="fanza",
        sources=sources,
        overwrite=True,
    )
    assert work.plot == "中文简介已存在"
    assert work.original_plot == "日本語のあらすじです"
    assert sources["original_plot"] == "fanza"
