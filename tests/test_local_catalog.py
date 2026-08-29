from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import AssetState, ContentFamily, MediaCategory, QueryMode
from shadow_mdc.identity import build_identity_hints
from shadow_mdc.services.local_catalog import infer_media_category
from shadow_mdc.services.path_filter import MediaPathFilter
from shadow_mdc.services.scanner import Scanner


def test_china_category_prevents_handle_from_becoming_jav_code() -> None:
    hints = build_identity_hints(
        "1025-#一个ren#Yigeren33推特作品.strm",
        category=MediaCategory.CHINA,
    )

    assert hints.code is None
    assert hints.family is ContentFamily.CHINESE
    assert hints.category is MediaCategory.CHINA


def test_infer_media_category_uses_bilingual_library_names() -> None:
    assert infer_media_category("JAV 有码") is MediaCategory.JAPAN
    assert infer_media_category("国产") is MediaCategory.CHINA
    assert infer_media_category("韩国收藏") is MediaCategory.KOREA
    assert infer_media_category("Europe") is MediaCategory.EUROPE
    assert infer_media_category("未分类") is MediaCategory.OTHER


def test_scanner_catalogs_dragon_generic_parts_without_review(tmp_path: Path) -> None:
    root = tmp_path / "China"
    folder = root / "DragonLLLLLL" / "Dragon's (@DragonLLLLL) 推特合集"
    folder.mkdir(parents=True)
    (folder / "V (1).strm").write_text("https://media.example/one.mp4", encoding="utf-8")
    (folder / "v2.strm").write_text("https://media.example/two.mp4", encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="China",
            root_path=str(root),
            category=MediaCategory.CHINA,
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )

        result = Scanner(repository).scan(library)
        assets = repository.list_assets()
        works = repository.list_works()

    assert result.cataloged == 2
    assert {asset.state for asset in assets} == {AssetState.IDENTIFIED.value}
    assert {work.title for work in works} == {"DragonLLLLL_1", "DragonLLLLL_2"}
    assert {work.category for work in works} == {MediaCategory.CHINA.value}
    assert {tuple(work.actors) for work in works} == {("DragonLLLLL",)}


def test_site_prefixed_generic_part_keeps_subject_and_sequence(tmp_path: Path) -> None:
    root = tmp_path / "China"
    folder = root / "一个ren"
    folder.mkdir(parents=True)
    (folder / "2048.cc@1(91).strm").write_text(
        "https://media.example/video.mp4",
        encoding="utf-8",
    )

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="China",
            root_path=str(root),
            category=MediaCategory.CHINA,
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )

        Scanner(repository).scan(library)
        works = repository.list_works()

    assert [work.title for work in works] == ["一个ren_1_91"]


def test_scanner_merges_jav_parts_by_code_and_uses_actor_directory(tmp_path: Path) -> None:
    root = tmp_path / "Japan"
    folder = root / "有码" / "河北彩花" / "[SONE-118]"
    folder.mkdir(parents=True)
    (folder / "SONE-118A.strm").write_text("https://media.example/a.mp4", encoding="utf-8")
    (folder / "SONE-118B.strm").write_text("https://media.example/b.mp4", encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="Japan",
            root_path=str(root),
            category=MediaCategory.JAPAN,
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )

        result = Scanner(repository).scan(library)
        assets = repository.list_assets()
        works = repository.list_works()

    assert result.cataloged == 2
    assert len(assets) == 2
    assert len(works) == 1
    assert works[0].primary_code == "SONE-118"
    assert works[0].category == MediaCategory.JAPAN.value
    assert works[0].actors == ["河北彩花"]
    assert {asset.work_id for asset in assets} == {works[0].id}


def test_filtered_existing_asset_is_hidden_without_deleting_file(tmp_path: Path) -> None:
    root = tmp_path / "China"
    root.mkdir()
    source = root / "广告片.strm"
    source.write_text("https://media.example/ad.mp4", encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="China",
            root_path=str(root),
            category=MediaCategory.CHINA,
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )
        Scanner(repository).scan(library)
        second = Scanner(repository, path_filter=MediaPathFilter(("广告片",))).scan(library)

        visible_assets = repository.list_assets()
        ignored_assets = repository.list_assets(state=AssetState.IGNORED.value)

    assert second.filtered == 1
    assert visible_assets == []
    assert len(ignored_assets) == 1
    assert source.is_file()
    assert ignored_assets[0].error == "filtered by path rule: 广告片"


def test_identity_hints_accept_explicit_category() -> None:
    hints = IdentityHints(
        term="title",
        mode=QueryMode.TEXT,
        title="title",
        category=MediaCategory.KOREA,
        family=ContentFamily.KOREAN,
    )

    assert hints.category is MediaCategory.KOREA
