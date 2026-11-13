"""Chinese subtitle backfill checks the primary stream and its language suffixes."""

from pathlib import Path

import httpx
import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.media.parts import subtitle_satisfies_chinese_backfill
from shadow_mdc.services.subtitle_backfill import (
    CHECKPOINT_KEY,
    TASK_KIND,
    SubtitleBackfillJob,
    iter_strm_dirs,
)
from shadow_mdc.services.subtitlecat import SubtitleCatFetcher

FIXTURES = Path(__file__).parent / "fixtures" / "subtitlecat"


@pytest.mark.parametrize(("filename", "expected"), [
    ("Film.srt", True), ("Film.forced.sdh.srt", True), ("Film.ZH_hAnT.SDH.ass", True),
    ("Film.en.zh-CN.srt", True), ("Film.zh.ja.srt", True), ("Film.双语.srt", True),
    ("Film.chs.srt", True), ("Film.cht.srt", True), ("Film.default.en.forced.srt", False),
    ("Film.pt-BR.ass", False), ("Film.ENG.jpn.srt", False), ("Film-CD2.zh.srt", False),
    ("Film2.zh.srt", False), ("Film.version.zh.srt", False),
])
def test_language_tags_belong_to_the_exact_media_stem(filename: str, expected: bool) -> None:
    assert subtitle_satisfies_chinese_backfill(Path(filename), "Film") is expected
    assert subtitle_satisfies_chinese_backfill(Path("Film.en.srt"), "Film.en")


def test_backfill_adds_chinese_alongside_foreign_and_unrelated_sidecars(tmp_path: Path) -> None:
    db = Database(f"sqlite:///{tmp_path / 'backfill.db'}")
    db.initialize()
    root = tmp_path / "library"
    root.mkdir()
    for name in ("SSIS-001.STRM", "SSIS-001-CD2.strm", "SSIS-001.en.forced.srt",
                 "SSIS-001.en.strm", "SSIS-001.en.zh.srt",
                 "SSIS-001-CD2.zh.srt", "SSIS-009.zh.srt"):
        (root / name).write_bytes(b"original")
    calls: list[str] = []
    subtitle = b"1\n00:00:01,000 --> 00:00:02,000\nHello\n"

    def fixture(req: httpx.Request) -> httpx.Response:
        calls.append(req.url.path)
        if req.url.path.endswith(".srt"):
            return httpx.Response(200, content=subtitle)
        page = "search_ssis001.html" if req.url.path == "/index.php" else "detail_ssis001.html"
        return httpx.Response(200, content=(FIXTURES / page).read_bytes())

    with httpx.Client(transport=httpx.MockTransport(fixture)) as client:
        fetcher = SubtitleCatFetcher(client=client, min_interval_seconds=0)
        job = SubtitleBackfillJob(database=db, fetcher_factory=lambda: fetcher)
        with db.session() as session:
            repo = Repository(session)
            task = repo.create_task_run(kind=TASK_KIND, scope=str(root))
            repo.save_checkpoint(CHECKPOINT_KEY, kind=TASK_KIND, status="running",
                                 cursor={"root": str(root)}, task_id=task.id)
            task_id = task.id
        assert job.run_sync(task_id) == "done"
        stats = job.status().stats
        assert (stats.written, stats.remote_lookups, stats.processed) == (1, 1, 1)
        assert (root / "SSIS-001.zh.srt").read_bytes() == subtitle
        assert (root / "SSIS-001.en.forced.srt").read_bytes() == b"original"
        assert (root / "SSIS-001-CD2.zh.srt").read_bytes() == b"original"
        assert (root / "SSIS-009.zh.srt").read_bytes() == b"original"
        calls.clear()
        assert fetcher.fetch("SSIS-001", root, "SSIS-001").status == "exists"
        assert not calls


def test_only_uppercase_stream_is_discovered(tmp_path: Path) -> None:
    folder = tmp_path / "stream"
    folder.mkdir()
    (folder / "film.StRm").touch()
    assert list(iter_strm_dirs(tmp_path)) == [folder]


def test_chinese_target_symlink_is_preserved_even_if_dangling(tmp_path: Path) -> None:
    target = tmp_path / "SSIS-001.zh.srt"
    target.symlink_to(tmp_path / "missing")
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected lookup"))) as client:
        assert SubtitleCatFetcher(client=client).fetch("SSIS-001", tmp_path, "SSIS-001").status == "exists"
    assert target.is_symlink() and not target.exists()


@pytest.mark.parametrize("other", ["Film.Zh.strm", "Film.mkv"])
def test_chinese_target_conflicting_with_another_media_stem_skips_lookup(tmp_path: Path, other: str) -> None:
    (tmp_path / "Film.strm").touch()
    (tmp_path / other).touch()
    with httpx.Client(transport=httpx.MockTransport(lambda _: pytest.fail("unexpected lookup"))) as client:
        result = SubtitleCatFetcher(client=client).fetch("SSIS-001", tmp_path, "Film")
    assert result.status == "ambiguous" and not (tmp_path / "Film.zh.srt").exists()
