from pathlib import Path

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints, ProviderRecord
from shadow_mdc.enums import AssetState, ContentFamily, MediaCategory, QueryMode
from shadow_mdc.identity import build_identity_hints
from shadow_mdc.services.local_catalog import infer_media_category
from shadow_mdc.services.path_filter import MediaPathFilter, default_filter_words
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
    assert infer_media_category("JAV 有码") is MediaCategory.OTHER
    assert infer_media_category("国产") is MediaCategory.CHINA
    assert infer_media_category("韩国收藏") is MediaCategory.KOREA
    assert infer_media_category("Europe") is MediaCategory.EUROPE
    assert infer_media_category("未分类") is MediaCategory.OTHER


def test_asset_classification_prefers_identity_then_language_and_path() -> None:
    jav = build_identity_hints(
        "SONE-118.mp4",
        context_names=("国产",),
    )
    chinese = build_identity_hints(
        "DragonLLLLLL_1.mp4",
        context_names=("Dragon's (@DragonLLLLL) 推特合集", "国产"),
    )
    western = build_identity_hints("Private beach party scene.mp4")
    korean = build_identity_hints("새로운 작품.mp4")
    japanese = build_identity_hints("新しい作品.mp4")
    camera_file = build_identity_hints("IMG_0717.strm", context_names=("国产",))
    platform_year = build_identity_hints(
        "ManyVids.2025.Asian.Creator.Scene.1080p.strm",
        context_names=("欧美",),
    )

    assert (jav.code, jav.family, jav.category) == (
        "SONE-118",
        ContentFamily.JAV,
        MediaCategory.JAPAN,
    )
    assert (chinese.code, chinese.family, chinese.category) == (
        None,
        ContentFamily.CHINESE,
        MediaCategory.CHINA,
    )
    assert western.category is MediaCategory.EUROPE
    assert korean.category is MediaCategory.KOREA
    assert japanese.category is MediaCategory.OTHER
    assert (camera_file.code, camera_file.category) == (None, MediaCategory.CHINA)
    assert (platform_year.code, platform_year.category) == (None, MediaCategory.EUROPE)


def test_control_directory_names_do_not_distort_script_classification() -> None:
    unknown = build_identity_hints(
        "v3-5.strm",
        context_names=("meowsex", "未知", "测试"),
        category=MediaCategory.OTHER,
    )
    japanese = build_identity_hints(
        "新しい作品.strm",
        context_names=("未知", "测试"),
        category=MediaCategory.OTHER,
    )

    assert unknown.category is MediaCategory.OTHER
    assert japanese.category is MediaCategory.OTHER


@pytest.mark.parametrize(
    ("name", "expected_family", "expected_category"),
    [
        (
            "怪兽33期-人妻佳奈的不伦性事 无水原版.strm",
            ContentFamily.CHINESE,
            MediaCategory.CHINA,
        ),
        ("naokaoxoxo.strm", ContentFamily.UNKNOWN, MediaCategory.OTHER),
        (
            "Я трахаю свою сводную сестру, застрявшую под кроватью.strm",
            ContentFamily.UNKNOWN,
            MediaCategory.OTHER,
        ),
    ],
)
def test_jav_directories_never_make_no_code_titles_jav(
    name: str,
    expected_family: ContentFamily,
    expected_category: MediaCategory,
) -> None:
    hints = build_identity_hints(
        name,
        context_names=("有码", "JAV"),
        category=MediaCategory.JAPAN,
    )

    assert hints.code is None
    assert hints.family is expected_family
    assert hints.category is expected_category


def test_jav_directory_still_accepts_a_real_code() -> None:
    hints = build_identity_hints(
        "SONE-118.mp4",
        context_names=("有码", "JAV"),
        category=MediaCategory.JAPAN,
    )

    assert hints.code == "SONE-118"
    assert hints.family is ContentFamily.JAV
    assert hints.category is MediaCategory.JAPAN


def test_scanner_queues_dragon_generic_parts_with_stable_titles(tmp_path: Path) -> None:
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
        candidates = [
            ProviderRecord.model_validate(item.record)
            for asset in assets
            for item in repository.list_candidates(asset.id)
        ]
        works = repository.list_works()

    assert result.queued == 2
    assert {asset.state for asset in assets} == {AssetState.REVIEW.value}
    assert works == []
    assert {record.title for record in candidates} == {"DragonLLLLL_1", "DragonLLLLL_2"}
    assert {record.category for record in candidates} == {MediaCategory.CHINA}
    assert {record.actors for record in candidates} == {("DragonLLLLL",)}


def test_scanner_prefixes_video_ranges_with_parent_subject(tmp_path: Path) -> None:
    root = tmp_path / "未知"
    folder = root / "meowsex"
    folder.mkdir(parents=True)
    for name in ("v.strm", "v6.strm", "v1-2.strm", "v3-5.strm", "v7-8.strm"):
        (folder / name).write_text(f"https://media.example/{name}", encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="测试",
            root_path=str(root),
            category=MediaCategory.OTHER,
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )

        result = Scanner(repository).scan(library)
        candidates = [
            ProviderRecord.model_validate(item.record)
            for asset in repository.list_assets()
            for item in repository.list_candidates(asset.id)
        ]
        hint_titles = {IdentityHints.model_validate(asset.hints).title for asset in repository.list_assets()}

    assert result.queued == 5
    assert {record.title for record in candidates} == {
        "meowsex",
        "meowsex_6",
        "meowsex_v1-2",
        "meowsex_v3-5",
        "meowsex_v7-8",
    }
    assert {record.actors for record in candidates} == {("meowsex",)}
    assert {record.category for record in candidates} == {MediaCategory.OTHER}
    assert hint_titles == {
        "meowsex",
        "meowsex_6",
        "meowsex_v1-2",
        "meowsex_v3-5",
        "meowsex_v7-8",
    }


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
        asset = repository.list_assets()[0]
        candidates = repository.list_candidates(asset.id)

    assert [ProviderRecord.model_validate(item.record).title for item in candidates] == ["一个ren_1_91"]


def test_rescan_keeps_accepted_local_work_identified(tmp_path: Path) -> None:
    root = tmp_path / "未知"
    folder = root / "meowsex"
    folder.mkdir(parents=True)
    (folder / "v3-5.strm").write_text("https://media.example/v3-5.mp4", encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="测试",
            root_path=str(root),
            category=MediaCategory.OTHER,
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )
        Scanner(repository).scan(library)
        asset = repository.list_assets()[0]
        work = repository.accept_local_candidate(asset.id)

        second = Scanner(repository).scan(library)
        refreshed = repository.list_assets()[0]

    assert work is not None
    assert second.queued == 0
    assert second.identified == 1
    assert refreshed.state == AssetState.IDENTIFIED.value
    assert refreshed.work_id == work.id
    assert work.title == "meowsex_v3-5"


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
        candidates = [
            ProviderRecord.model_validate(item.record)
            for asset in assets
            for item in repository.list_candidates(asset.id)
        ]
        works = repository.list_works()

    assert result.queued == 2
    assert len(assets) == 2
    assert works == []
    assert {record.code for record in candidates} == {"SONE-118"}
    assert {record.category for record in candidates} == {MediaCategory.JAPAN}
    assert {record.actors for record in candidates} == {("河北彩花",)}
    assert {asset.state for asset in assets} == {AssetState.REVIEW.value}


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


@pytest.mark.parametrize(
    "name",
    (
        "x u u 6 2 . c o m.strm",
        "有 趣 的 臺 灣 妹 妹 直 播.strm",
        "有趣的小视频.strm",
    ),
)
def test_default_filter_catches_spaced_jav_directory_ads(tmp_path: Path, name: str) -> None:
    root = tmp_path / "library"
    path = root / "URE-088" / name

    match = MediaPathFilter(default_filter_words()).match(path, root)

    assert match is not None


def test_identity_hints_accept_explicit_category() -> None:
    hints = IdentityHints(
        term="title",
        mode=QueryMode.TEXT,
        title="title",
        category=MediaCategory.KOREA,
        family=ContentFamily.KOREAN,
    )

    assert hints.category is MediaCategory.KOREA
