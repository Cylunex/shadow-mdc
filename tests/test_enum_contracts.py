"""Lifecycle, presence and query axes must not share incompatible meanings."""

from collections.abc import Callable
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.catalog_management import OrganizationCreate, OrganizationLink
from shadow_mdc.db.models import MediaAsset, MediaEmbyBinding, TaskRun, Work
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.enums import TERMINAL_TASK_STATUSES, LibraryPresence, TaskStatus
from shadow_mdc.intake_core import analyze_magnet
from shadow_mdc.providers.javdb_api import RankingPeriod, RankingZone, zone_code
from shadow_mdc.services.discover import javdb_ranking_spec
from shadow_mdc.services.jobs import JobManager
from shadow_mdc.skill_api import BrowseInput


@pytest.mark.parametrize("status", sorted(TERMINAL_TASK_STATUSES))
def test_late_progress_does_not_resurrect_a_finished_task(tmp_path: Path, status: TaskStatus) -> None:
    database = Database(f"sqlite:///{tmp_path / 'tasks.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        task = repo.create_task_run(kind="scan", scope="fixture")
        repo.finish_task_run(task, status=status, summary={"processed": 2})
        repo.update_task_progress(task, {"processed": 1, "phase": "running"})
        assert task.status == status and task.summary == {"processed": 2}
        with pytest.raises(ValueError, match="finished"):
            repo.request_task_cancel(task)


@pytest.mark.parametrize("invalid", ["running", "cancel_requested", "done", "queued", "unexpected"])
def test_finishing_a_general_task_rejects_nonterminal_and_other_domain_statuses(
    tmp_path: Path,
    invalid: str,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'tasks.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        task = repo.create_task_run(kind="scan", scope="fixture")
        with pytest.raises(ValueError):
            repo.finish_task_run(task, status=invalid, summary={})
        assert task.status == "running" and task.finished_at is None
        repo.request_task_cancel(task)
        repo.update_task_progress(task, {"processed": 1})
        assert task.status == "cancel_requested" and task.summary["processed"] == 1


async def test_unknown_job_result_is_failed_rather_than_reported_success(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'jobs.db'}")
    database.initialize()
    manager = JobManager(database)

    async def body(
        repo: Repository,
        progress: Callable[[dict[str, object]], None],
        cancelled: Callable[[], bool],
    ) -> dict[str, object]:
        return {"status": "unexpected"}

    identity = await manager.start(kind="fixture", scope="fixture", body=body)
    running = manager._jobs[identity].asyncio_task
    assert running is not None
    await running
    with database.session() as session:
        task = session.get(TaskRun, identity)
        assert task is not None and task.status == "failed" and task.finished_at is not None


@pytest.mark.parametrize(
    "status,expected",
    [
        ("running", "running"),
        ("done", "done"),
        ("completed", "done"),
        ("failed", "failed"),
        ("cancelled", "cancelled"),
        ("future-status", "unknown"),
    ],
)
def test_offline_completion_is_independent_of_registered_media(
    tmp_path: Path,
    status: str,
    expected: str,
) -> None:
    database = Database(f"sqlite:///{tmp_path / 'intake.db'}")
    database.initialize()
    magnet = "magnet:?xt=urn:btih:" + "A" * 40
    with database.session() as session:
        repo = Repository(session)
        session.add(Work(id="work", title="Fixture"))
        session.flush()
        repo.create_pan_offline_task(
            work_id="work",
            url=magnet,
            info_hash="A" * 40,
            directory_id="fixture",
            remote_task_id="fixture",
            status=status,
        )
        inspected = analyze_magnet(repo, magnet=magnet)
        assert inspected.offline_status == expected
        assert inspected.presence is LibraryPresence.CATALOG_ONLY
        library = repo.create_library(
            name="Fixture", root_path=str(tmp_path), recursive=True, organize_template="{code}"
        )
        session.add(
            MediaAsset(
                library_id=library.id, work_id="work", path=str(tmp_path / "video.mp4"), size=1, hints={}
            )
        )
        session.flush()
        assert analyze_magnet(repo, magnet=magnet).presence is LibraryPresence.IN_LIBRARY


def test_wanted_and_media_availability_compose_in_queries_and_dynamic_playlists(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'filters.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as client:
        runtime = app.state.runtime
        with runtime.database.session() as session:
            repo = Repository(session)
            library = repo.create_library(
                name="Fixture", root_path=str(tmp_path), recursive=True, organize_template="{code}"
            )
            session.add_all([Work(id=identity, title=identity) for identity in "abcde"])
            session.flush()
            session.add_all(
                [
                    MediaAsset(
                        library_id=library.id,
                        work_id=identity,
                        path=str(tmp_path / f"{identity}.mp4"),
                        size=1,
                        hints={},
                    )
                    for identity in "ac"
                ]
            )
            session.add(MediaEmbyBinding(work_id="d", server_id="fixture", item_id="fixture"))
        prefs = runtime.library_prefs_store.load()
        runtime.library_prefs_store.save(prefs.model_copy(update={"want_list": ["a", "b"]}))
        for filters, expected in [
            ({"wanted": True, "availability": "files"}, {"a"}),
            ({"wanted": True, "availability": "metadata"}, {"b"}),
            ({"wanted": False, "availability": "files"}, {"c"}),
            ({"wanted": False, "availability": "emby"}, {"d"}),
            ({"status": "want", "availability": "files"}, {"a"}),
            ({"status": "downloaded", "wanted": False}, {"c"}),
        ]:
            page = client.post("/api/catalog/works/query", json=filters)
            assert page.status_code == 200, page.text
            assert {work["id"] for work in page.json()["items"]} == expected
        playlist = client.post(
            "/api/catalog/playlists",
            headers={"X-Catalog-Owner": "fixture"},
            json={
                "name": "Wanted with media",
                "filters": {"wanted": True, "availability": "files"},
            },
        )
        assert playlist.status_code == 201 and playlist.json()["work_ids"] == ["a"]
        assert client.get("/api/discover/browse?list=rankings_made_up").status_code == 422


def test_organization_form_and_credit_role_are_different_domains() -> None:
    with pytest.raises(ValueError):
        OrganizationCreate(name="Fixture", kind="distributor")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        OrganizationLink(work_id="work", role="organization")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        BrowseInput(list="rankings_made_up")  # type: ignore[arg-type]


def test_ranking_wire_codes_and_legacy_list_names_preserve_separate_axes() -> None:
    assert [zone_code(zone) for zone in RankingZone] == [0, 1, 2, 3]
    for period in RankingPeriod:
        for zone in RankingZone:
            suffix = "" if zone is RankingZone.CENSORED else f"_{zone}"
            parsed = javdb_ranking_spec(f"rankings_{period}{suffix}")
            assert parsed is not None and parsed.zone is zone and parsed.period is period
    for invalid in ("rankings_yearly", "rankings_fc2_daily", "rankings_daily_unknown", "latest"):
        assert javdb_ranking_spec(invalid) is None
