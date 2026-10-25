"""Batch 2: resumable jobs, subtitle backfill, organize undo journal."""

import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.media.organize_journal import JournalEntry, OrganizeJournal, ReverseAction, undo_entries
from shadow_mdc.services.resumable_jobs import mark_interrupted_task_runs
from shadow_mdc.services.subtitle_backfill import (
    CHECKPOINT_KEY,
    SubtitleBackfillJob,
    directory_code,
    iter_strm_dirs,
    primary_strm,
)
from shadow_mdc.services.subtitlecat import SubtitleFetchResult


def _db(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'jobs.db'}")
    database.initialize()
    return database


@dataclass
class FakeFetcher:
    misses: set[str] = field(default_factory=set)
    calls: list[str] = field(default_factory=list)

    def fetch(self, code: str, directory: Path, stem: str, *, dry_run: bool = False) -> SubtitleFetchResult:
        self.calls.append(code)
        if code in self.misses:
            return SubtitleFetchResult(code=code, status="miss")
        target = directory / f"{stem}.zh.srt"
        target.write_text("1\n00:00:01,000 --> 00:00:02,000\nhi\n", encoding="utf-8")
        return SubtitleFetchResult(code=code, status="written", path=target)


def _strm_library(root: Path) -> None:
    for code in ("ABP-001", "ABP-002", "ABP-003", "ABP-004"):
        folder = root / "JAV" / f"[{code}] title"
        folder.mkdir(parents=True)
        (folder / f"{code}.strm").write_text("http://x", encoding="utf-8")
    multi = root / "JAV" / "[ABP-002] title"
    (multi / "ABP-002_1.strm").write_text("http://x", encoding="utf-8")
    (root / "JAV" / "[ABP-003] title" / "ABP-003.chs.srt").write_text("old", encoding="utf-8")


def test_strm_dir_helpers(tmp_path: Path) -> None:
    _strm_library(tmp_path)
    dirs = list(iter_strm_dirs(tmp_path))
    assert len(dirs) == 4
    multi = tmp_path / "JAV" / "[ABP-002] title"
    strm = primary_strm(multi)
    assert strm is not None and strm.stem == "ABP-002"
    assert directory_code(multi, strm) == "ABP-002"


def test_backfill_is_resumable_and_skips_existing(tmp_path: Path) -> None:
    root = tmp_path / "strm"
    _strm_library(root)
    database = _db(tmp_path)
    fetcher = FakeFetcher(misses={"ABP-004"})
    job = SubtitleBackfillJob(database=database, fetcher_factory=lambda: fetcher)

    with database.session() as session:
        repo = Repository(session)
        task = repo.create_task_run(kind="subtitles:backfill", scope=str(root))
        repo.save_checkpoint(
            CHECKPOINT_KEY,
            kind="subtitle-backfill",
            status="running",
            cursor={"root": str(root), "limit": 1},
            task_id=task.id,
        )
        first_task = task.id
    assert job.run_sync(first_task) == "paused"
    assert fetcher.calls == ["ABP-001"]
    status = job.status()
    assert status.status == "paused" and status.stats.written == 1
    assert status.last_dir is not None and status.last_dir.endswith("[ABP-001] title")

    # Resume without limit: continues after ABP-001, skips ABP-003 (has .srt).
    with database.session() as session:
        repo = Repository(session)
        task = repo.create_task_run(kind="subtitles:backfill", scope=str(root))
        repo.save_checkpoint(
            CHECKPOINT_KEY,
            kind="subtitle-backfill",
            status="running",
            cursor={"root": str(root), "last_dir": status.last_dir, "limit": None},
            task_id=task.id,
        )
        second_task = task.id
    assert job.run_sync(second_task) == "done"
    assert fetcher.calls == ["ABP-001", "ABP-002", "ABP-004"]
    final = job.status()
    assert final.status == "done"
    assert final.stats.written == 2
    assert final.stats.exists == 1
    assert final.stats.miss == 1
    assert final.stats.processed == 4
    assert (root / "JAV" / "[ABP-002] title" / "ABP-002.zh.srt").is_file()
    assert not (root / "JAV" / "[ABP-002] title" / "ABP-002_1.zh.srt").exists()
    with database.session() as session:
        finished = Repository(session).get_task_run(second_task)
        assert finished is not None and finished.status == "succeeded"


def test_backfill_stops_after_consecutive_errors(tmp_path: Path) -> None:
    root = tmp_path / "strm"
    for index in range(12):
        folder = root / f"ABP-{index + 100:03d}"
        folder.mkdir(parents=True)
        (folder / f"ABP-{index + 100:03d}.strm").write_text("x", encoding="utf-8")
    database = _db(tmp_path)

    class Broken:
        def fetch(
            self, code: str, directory: Path, stem: str, *, dry_run: bool = False
        ) -> SubtitleFetchResult:
            return SubtitleFetchResult(code=code, status="error", detail="boom")

    job = SubtitleBackfillJob(database=database, fetcher_factory=Broken)
    with database.session() as session:
        repo = Repository(session)
        task = repo.create_task_run(kind="subtitles:backfill", scope=str(root))
        repo.save_checkpoint(
            CHECKPOINT_KEY,
            kind="subtitle-backfill",
            status="running",
            cursor={"root": str(root)},
            task_id=task.id,
        )
        task_id = task.id
    assert job.run_sync(task_id) == "paused"
    status = job.status()
    assert status.stats.errors == 10
    assert status.reason is not None and "consecutive" in status.reason


def test_mark_interrupted_task_runs(tmp_path: Path) -> None:
    database = _db(tmp_path)
    with database.session() as session:
        repo = Repository(session)
        scan = repo.create_task_run(kind="scan", scope="/x")
        other = repo.create_task_run(kind="organize:move", scope="/x")
        done = repo.create_task_run(kind="scan", scope="/x")
        repo.finish_task_run(done, status="succeeded", summary={})
        ids = (scan.id, other.id, done.id)
    closed = mark_interrupted_task_runs(database)
    assert sorted(closed) == sorted(ids[:2])
    with database.session() as session:
        repo = Repository(session)
        scan_row = repo.get_task_run(ids[0])
        other_row = repo.get_task_run(ids[1])
        assert scan_row is not None and scan_row.status == "interrupted"
        assert scan_row.summary["resumable"] is True
        assert other_row is not None and other_row.summary["resumable"] is False


def test_undo_refuses_to_clobber_changed_files(tmp_path: Path) -> None:
    source = tmp_path / "a" / "x.strm"
    moved = tmp_path / "b" / "x.strm"
    moved.parent.mkdir()
    moved.write_text("m", encoding="utf-8")
    source.parent.mkdir()
    source.write_text("someone else", encoding="utf-8")  # original slot re-occupied
    created = tmp_path / "b" / "poster.jpg"
    created.write_bytes(b"12345")
    journal = OrganizeJournal()
    journal.moved(source, moved, asset_id="a1", detail="media")
    journal.created(created, detail="artwork:poster")
    created.write_bytes(b"edited by user")
    report = undo_entries(journal.entries, dry_run=False)
    assert report.done == 0 and report.skipped == 2
    assert moved.is_file() and created.is_file()


def test_undo_restores_previous_nfo_text(tmp_path: Path) -> None:
    nfo = tmp_path / "movie.nfo"
    nfo.write_text("<movie>old</movie>", encoding="utf-8")
    journal = OrganizeJournal()
    assert journal.before_overwrite(nfo, detail="nfo")
    nfo.write_text("<movie>new</movie>", encoding="utf-8")
    preview = undo_entries(journal.entries, dry_run=True)
    assert preview.steps[0].status == "would-do"
    assert nfo.read_text(encoding="utf-8") == "<movie>new</movie>"
    undo_entries([JournalEntry.model_validate(e) for e in journal.dump()], dry_run=False)
    assert nfo.read_text(encoding="utf-8") == "<movie>old</movie>"
    assert journal.entries[0].action is ReverseAction.RESTORE_TEXT


def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, name: str) -> Path:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / name}")
    return data_dir


def test_organize_move_then_one_click_undo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_dir = tmp_path / "incoming"
    source_dir = media_dir / "batch"
    target_dir = tmp_path / "organized"
    (source_dir / "ads").mkdir(parents=True)
    target_dir.mkdir()
    source = source_dir / "SONE-118.strm"
    source.write_text("https://media.example/SONE-118.mp4", encoding="utf-8")
    junk = source_dir / "广告片.txt"
    junk.write_text("junk", encoding="utf-8")
    _env(tmp_path, monkeypatch, "undo.db")

    with TestClient(app) as client:
        assert client.put("/api/settings/filter-words", json={"words": ["广告片"]}).status_code == 200
        library = client.post("/api/libraries", json={"name": "U", "root_path": str(media_dir)}).json()
        client.post(f"/api/libraries/{library['id']}/scan")
        asset = client.get("/api/assets").json()[0]
        candidate = client.get(f"/api/assets/{asset['id']}/candidates").json()[0]
        assert client.post(f"/api/candidates/{candidate['id']}/accept").status_code == 200
        plan = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "move", "target_root": str(target_dir)},
        ).json()
        applied = client.post(
            f"/api/libraries/{library['id']}/organize/apply",
            json={
                "mode": "move",
                "target_root": str(target_dir),
                "token": plan["token"],
                "nfo_policy": "replace",
            },
        )
        assert applied.status_code == 200 and applied.json()["succeeded"] == 1
        assert not source.exists() and not junk.exists()

        task = next(t for t in client.get("/api/tasks").json() if t["kind"] == "organize:move")
        assert task["summary"]["undo_entries"] > 0
        journal = client.get(f"/api/organize/runs/{task['id']}/journal")
        assert journal.status_code == 200
        actions = {entry["action"] for entry in journal.json()["entries"]}
        assert {"move_back", "delete_created", "restore_trashed"} <= actions

        preview = client.post(f"/api/organize/runs/{task['id']}/undo", json={"dry_run": True}).json()
        assert preview["dry_run"] is True and preview["errors"] == 0
        assert not source.exists()

        undone = client.post(f"/api/organize/runs/{task['id']}/undo", json={"dry_run": False}).json()
        assert undone["errors"] == 0, undone
        assert source.is_file() and junk.is_file()
        assert not any(path.is_file() for path in target_dir.rglob("*"))
        assert Path(client.get("/api/assets").json()[0]["path"]) == source.resolve()
        again = client.post(f"/api/organize/runs/{task['id']}/undo", json={"dry_run": False})
        assert again.status_code == 409


def test_backfill_api_requires_enable_and_reports_status(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env(tmp_path, monkeypatch, "bf.db")
    monkeypatch.setenv("SHADOW_MDC_SUBTITLECAT_ENABLED", "false")
    with TestClient(app) as client:
        status = client.get("/api/subtitles/backfill")
        assert status.status_code == 200 and status.json()["status"] == "idle"
        assert client.post("/api/subtitles/backfill", json={"root": str(tmp_path)}).status_code == 409


def test_backfill_api_runs_with_fake_fetcher(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "strm"
    _strm_library(root)
    _env(tmp_path, monkeypatch, "bf2.db")
    monkeypatch.setenv("SHADOW_MDC_SUBTITLECAT_ENABLED", "true")
    fetcher = FakeFetcher()
    with TestClient(app) as client:
        job = app.state.runtime.subtitle_backfill
        job.fetcher_factory = lambda: fetcher
        started = client.post("/api/subtitles/backfill", json={"root": str(root), "limit": 2})
        assert started.status_code == 200, started.text
        for _ in range(200):
            body = client.get("/api/subtitles/backfill").json()
            if not body["running"]:
                break
            time.sleep(0.02)
        assert body["status"] == "paused" and body["stats"]["remote_lookups"] == 2
        resumed = client.post(f"/api/tasks/{body['task_id']}/resume")
        assert resumed.status_code == 200
        for _ in range(200):
            body = client.get("/api/subtitles/backfill").json()
            if not body["running"]:
                break
            time.sleep(0.02)
        assert body["status"] == "done"
        assert fetcher.calls == ["ABP-001", "ABP-002", "ABP-004"]


def test_resume_interrupted_scan(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "SONE-118.strm").write_text("x", encoding="utf-8")
    _env(tmp_path, monkeypatch, "resume.db")
    with TestClient(app) as client:
        library = client.post("/api/libraries", json={"name": "R", "root_path": str(media_dir)}).json()
        database = app.state.runtime.database
        with database.session() as session:
            repo = Repository(session)
            task = repo.create_task_run(kind="scan", scope=library["root_path"])
            task_id = task.id
        mark_interrupted_task_runs(database)
        resumed = client.post(f"/api/tasks/{task_id}/resume")
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["kind"] == "scan"
        assert len(client.get("/api/assets").json()) == 1


def test_backfill_survives_restart(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    root = tmp_path / "strm"
    _strm_library(root)
    data_dir = _env(tmp_path, monkeypatch, "restart.db")
    monkeypatch.setenv("SHADOW_MDC_SUBTITLECAT_ENABLED", "true")

    class Slow(FakeFetcher):
        def fetch(
            self, code: str, directory: Path, stem: str, *, dry_run: bool = False
        ) -> SubtitleFetchResult:
            time.sleep(0.2)
            return super().fetch(code, directory, stem, dry_run=dry_run)

    first = Slow()
    with TestClient(app) as client:
        app.state.runtime.subtitle_backfill.fetcher_factory = lambda: first
        assert client.post("/api/subtitles/backfill", json={"root": str(root)}).status_code == 200
        time.sleep(0.05)
    # Process "restarted": checkpoint still marked running, so startup resumes it.
    database = Database(f"sqlite:///{data_dir / 'restart.db'}")
    with database.session() as session:
        checkpoint = Repository(session).get_checkpoint(CHECKPOINT_KEY)
        assert checkpoint is not None and checkpoint.status == "running"
    second = FakeFetcher()
    monkeypatch.setattr("shadow_mdc.config.Settings.subtitlecat_fetcher", lambda self: second)
    with TestClient(app) as client:
        body = client.get("/api/subtitles/backfill").json()
        assert body["status"] in {"running", "done"}
        for _ in range(100):
            body = client.get("/api/subtitles/backfill").json()
            if not body["running"]:
                break
            time.sleep(0.05)
        assert body["status"] == "done"
        assert body["stats"]["written"] == 3
        assert len(first.calls) + len(second.calls) == 3
