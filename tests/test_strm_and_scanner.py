from pathlib import Path

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints, MediaTechnicalInfo, ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory, QueryMode
from shadow_mdc.media.strm import read_strm_locator, redact_media_locator
from shadow_mdc.services import scanner as scanner_module
from shadow_mdc.services.alias_store import default_alias_rules
from shadow_mdc.services.non_jav_actor_catalog import parse_non_jav_actor_text
from shadow_mdc.services.scanner import Scanner


@pytest.mark.parametrize(
    ("content", "encoding", "expected"),
    [
        ('"https://media.example/SSIS-123.mp4"\n', "utf-8", "https://media.example/SSIS-123.mp4"),
        ("https://媒体.example/作品.mp4\n", "utf-16", "https://媒体.example/作品.mp4"),
        ("# comment\nhttps://media.example/scene.m3u8\n", "gb18030", "https://media.example/scene.m3u8"),
    ],
)
def test_read_strm_locator_encodings(
    tmp_path: Path,
    content: str,
    encoding: str,
    expected: str,
) -> None:
    path = tmp_path / "item.strm"
    path.write_bytes(content.encode(encoding))

    assert read_strm_locator(path) == expected


def test_read_strm_locator_resolves_relative_target(tmp_path: Path) -> None:
    path = tmp_path / "item.strm"
    path.write_text("../remote/movie.mp4\n", encoding="utf-8")

    assert read_strm_locator(path) == str((tmp_path / "../remote/movie.mp4").resolve())


def test_redact_media_locator_removes_credentials_and_tokens() -> None:
    locator = "https://user:password@media.example:8443/path/movie.mp4?token=secret#part"

    assert redact_media_locator(locator) == "https://media.example:8443/path/movie.mp4"


@pytest.mark.parametrize("content", ["", "# only a comment\n", "javascript:alert(1)\n"])
def test_read_strm_locator_rejects_invalid_content(tmp_path: Path, content: str) -> None:
    path = tmp_path / "invalid.strm"
    path.write_text(content, encoding="utf-8")

    with pytest.raises(ValueError):
        read_strm_locator(path)


def test_scanner_indexes_strm_without_opening_remote_media(tmp_path: Path) -> None:
    root = tmp_path / "library"
    folder = root / "小宝探花"
    folder.mkdir(parents=True)
    strm = folder / "001.strm"
    locator = "https://media.example/真实标题.mp4?token=private"
    strm.write_text(locator, encoding="utf-8")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="STRM",
            root_path=str(root),
            recursive=True,
            organize_template="{series}/{code_or_title}.{ext}",
        )

        result = Scanner(repository, default_alias_rules()).scan(library)
        assets = repository.list_assets()

    assert result.discovered == 1
    assert result.errors == ()
    assert len(assets) == 1
    hints = IdentityHints.model_validate(assets[0].hints)
    assert hints.mode is QueryMode.TEXT
    assert hints.title == "真实标题"
    assert hints.family is ContentFamily.CHINESE
    assert hints.series == "小宝探花"
    assert hints.media_locator == "https://media.example/真实标题.mp4"
    assert assets[0].duration_seconds is None
    assert assets[0].oshash is None


def test_scanner_uses_parent_for_generic_no_code_filename(tmp_path: Path) -> None:
    root = tmp_path / "library"
    folder = root / "杏吧原创" / "自定义系列"
    folder.mkdir(parents=True)
    (folder / "01.mp4").write_bytes(b"fixture")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="No code",
            root_path=str(root),
            recursive=True,
            organize_template="{studio}/{code_or_title}.{ext}",
        )

        result = Scanner(repository, default_alias_rules()).scan(library)
        hints = IdentityHints.model_validate(repository.list_assets()[0].hints)

    assert result.discovered == 1
    assert hints.mode is QueryMode.TEXT
    assert hints.title == "自定义系列"
    assert hints.studio == "杏吧传媒"
    assert hints.family is ContentFamily.CHINESE


def test_scanner_auto_catalogs_numbered_files_in_known_non_jav_actor_directory(
    tmp_path: Path,
) -> None:
    root = tmp_path / "library"
    folder = root / "Europe" / "Abella Danger"
    folder.mkdir(parents=True)
    (folder / "1.mp4").write_bytes(b"fixture")
    catalog = parse_non_jav_actor_text(
        "**欧美热门女优**\nAbella Danger,Abella Danger",
        source="fixture.txt",
    )

    database = Database(f"sqlite:///{tmp_path / 'known-actor.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="Known actor",
            root_path=str(root),
            recursive=True,
            organize_template="{actor}/{code_or_title}.{ext}",
        )
        result = Scanner(
            repository,
            default_alias_rules(),
            non_jav_actor_catalog=catalog,
        ).scan(library)
        assets = repository.list_assets()
        work = repository.get_work(assets[0].work_id or "")

    assert result.queued == 0
    assert result.identified == 1
    assert work is not None
    assert work.title == "Abella Danger_1"
    assert work.actors == ["Abella Danger"]
    assert work.category == MediaCategory.EUROPE.value


def test_bad_strm_is_reported_without_stopping_scan(tmp_path: Path) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "empty.strm").write_text("", encoding="utf-8")
    (root / "SSIS-123.mp4").write_bytes(b"fixture")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="Mixed",
            root_path=str(root),
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )

        result = Scanner(repository).scan(library)

    assert result.discovered == 1
    assert len(result.errors) == 1
    assert "empty.strm" in result.errors[0]


def test_unchanged_video_reuses_probe_and_hash(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    (root / "SSIS-123.mp4").write_bytes(b"fixture")
    probes = 0
    hashes = 0

    def probe(path: Path) -> MediaTechnicalInfo:
        nonlocal probes
        probes += 1
        return MediaTechnicalInfo(duration_seconds=123.0)

    def oshash(path: Path) -> str:
        nonlocal hashes
        hashes += 1
        return "0123456789abcdef"

    monkeypatch.setattr(scanner_module, "probe_media_info", probe)
    monkeypatch.setattr(scanner_module, "compute_oshash", oshash)
    database = Database(f"sqlite:///{tmp_path / 'reuse.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="Reuse",
            root_path=str(root),
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )
        Scanner(repository).scan(library)
        Scanner(repository).scan(library)

    assert probes == 1
    assert hashes == 1


def test_scanner_reuses_saved_work_and_only_refreshes_file_quality(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    source = root / "SONE-118.mp4"
    source.write_bytes(b"first-quality")
    qualities = iter(
        (
            MediaTechnicalInfo(width=1920, height=1080, quality_label="1080p"),
            MediaTechnicalInfo(width=3840, height=2160, quality_label="4K"),
        )
    )
    monkeypatch.setattr(scanner_module, "probe_media_info", lambda path: next(qualities))
    monkeypatch.setattr(scanner_module, "compute_oshash", lambda path: None)

    database = Database(f"sqlite:///{tmp_path / 'reuse-work.db'}")
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
        work = repository.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="sone-118",
                code="SONE-118",
                title="Saved stable title",
                family=ContentFamily.JAV,
            ),
            overwrite=True,
        )

        first = Scanner(repository).scan(library)
        source.write_bytes(b"second-quality-is-larger")
        second = Scanner(repository).scan(library)
        asset = repository.list_assets()[0]
        media_info = MediaTechnicalInfo.model_validate(asset.media_info)

    assert first.identified == 1
    assert second.identified == 1
    assert asset.work_id == work.id
    assert asset.state == "identified"
    assert media_info.quality_label == "4K"
    assert work.title == "Saved stable title"


def test_scanner_ignores_metadata_and_recycle_directories(tmp_path: Path) -> None:
    root = tmp_path / "library"
    (root / ".actors").mkdir(parents=True)
    (root / "#recycle").mkdir()
    (root / ".actors" / "SSIS-001.mp4").write_bytes(b"ignored")
    (root / "#recycle" / "SSIS-002.mp4").write_bytes(b"ignored")
    (root / "SSIS-003.mp4").write_bytes(b"kept")
    database = Database(f"sqlite:///{tmp_path / 'ignore-dirs.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="Ignored dirs",
            root_path=str(root),
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )
        result = Scanner(repository).scan(library)

    assert result.discovered == 1
