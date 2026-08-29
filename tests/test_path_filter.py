from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.path_filter import (
    FilterWords,
    FilterWordsStore,
    MediaPathFilter,
    default_filter_words,
)
from shadow_mdc.services.scanner import Scanner


def test_media_path_filter_matches_compact_chinese_and_ascii_tokens(tmp_path: Path) -> None:
    root = tmp_path / "library"
    path_filter = MediaPathFilter(("社 區 最 新 情 報", "sample"))

    chinese = path_filter.match(root / "广告" / "社區最新情報-01.mp4", root)
    numbered_sample = path_filter.match(root / "sample01.mkv", root)

    assert chinese is not None
    assert chinese.word == "社 區 最 新 情 報"
    assert numbered_sample is not None
    assert path_filter.match(root / "Example-001.mp4", root) is None


def test_filter_words_store_uses_defaults_and_saves_clean_lines(tmp_path: Path) -> None:
    path = tmp_path / "data" / "filter-words.txt"
    store = FilterWordsStore(path)

    assert store.load().words == default_filter_words()

    saved = store.save(FilterWords(words=(" sample ", "sample", "广告文案")))

    assert saved.words == ("sample", "广告文案")
    assert path.read_text(encoding="utf-8") == "sample\n广告文案\n"
    assert store.load() == saved


def test_scanner_counts_filtered_media_separately_from_skipped_files(tmp_path: Path) -> None:
    root = tmp_path / "library"
    junk_folder = root / "社區最新情報"
    junk_folder.mkdir(parents=True)
    (junk_folder / "001.mp4").write_bytes(b"junk")
    (root / "trailer.mp4").write_bytes(b"junk")
    (root / "Example-001.mp4").write_bytes(b"movie")
    (root / "SSIS-123.mkv").write_bytes(b"movie")
    (root / "poster.jpg").write_bytes(b"image")

    database = Database(f"sqlite:///{tmp_path / 'test.db'}")
    database.initialize()
    with database.session() as session:
        repository = Repository(session)
        library = repository.create_library(
            name="Filtered",
            root_path=str(root),
            recursive=True,
            organize_template="{code_or_title}.{ext}",
        )

        result = Scanner(
            repository,
            path_filter=MediaPathFilter(default_filter_words()),
        ).scan(library)
        assets = repository.list_assets()

    assert result.discovered == 2
    assert result.filtered == 2
    assert result.skipped == 1
    assert {Path(asset.path).name for asset in assets} == {"Example-001.mp4", "SSIS-123.mkv"}
