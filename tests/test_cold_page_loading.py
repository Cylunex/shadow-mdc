from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import event, insert, update

from shadow_mdc.api import Runtime, app
from shadow_mdc.api_models import WorkOut
from shadow_mdc.db.models import Actor, ExternalIdentity, Work, WorkActor
from shadow_mdc.db.repository import Repository
from shadow_mdc.identity import normalize_identity_value
from shadow_mdc.media.artwork import resolve_work_display_images
from shadow_mdc.services.actor_catalog import ActorCatalogStore
from shadow_mdc.services.javranking_client import CuratedList, CuratedVideoEntry, JavRankingIndexCache


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'cold.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as session:
        runtime: Runtime = app.state.runtime
        with runtime.database.session() as database:
            database.execute(
                insert(Actor), [{"id": "actor", "name": "Fixture Actor", "normalized_name": "fixture actor"}]
            )
            database.execute(
                insert(Work),
                [
                    {
                        "id": f"work-{i}",
                        "title": f"Fixture {i}",
                        "primary_code": f"ABC-{i + 1:03d}",
                        "family": "jav",
                        "category": "Japan",
                        "actors": ["Fixture Actor"],
                        "tags": ["Tag"],
                        "plot": "Large description. " * 100,
                        "artwork": [{"kind": "poster", "url": f"https://example.com/{i}.jpg"}],
                    }
                    for i in range(30)
                ],
            )
            database.execute(
                insert(WorkActor),
                [{"work_id": f"work-{i}", "actor_id": "actor", "position": 0} for i in range(30)],
            )
        yield session


def test_compact_cards_restore_the_same_models_and_use_separate_cache(client: TestClient) -> None:
    regular = client.get("/api/works")
    compact = client.get("/api/works?compact=true")
    regular.raise_for_status()
    compact.raise_for_status()
    assert [WorkOut.model_validate(row) for row in regular.json()] == [
        WorkOut.model_validate(row) for row in compact.json()
    ]
    assert len(compact.content) < len(regular.content) * 0.8
    assert client.get("/api/works").json() == regular.json()
    assert client.get("/api/works?compact=true").json() == compact.json()


def test_actor_cards_are_read_only_and_keep_full_counts_and_search_codes(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden(self: ActorCatalogStore, *args: object) -> None:
        raise AssertionError("cold list GET must not serialize or write the durable catalog")

    monkeypatch.setattr(ActorCatalogStore, "save", forbidden)
    app.state.runtime.response_cache.clear()
    response = client.get("/api/actors")
    response.raise_for_status()
    actor = next(row for row in response.json() if row["id"] == "actor")
    assert actor["work_count"] == 30 and len(actor["works"]) == 8
    assert len(actor["work_codes"]) == 30 and "ABC-030" in actor["work_codes"]
    assert all(work["image_url"] for work in actor["works"])


def test_cover_batch_skips_per_work_directory_stats_and_sees_new_artwork(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    works = [Work(id=f"work-{i}", title="Fixture", artwork=[]) for i in range(100)]
    with monkeypatch.context() as scoped:
        scoped.setattr(Path, "is_dir", lambda self: pytest.fail("one is_dir per work is forbidden"))
        assert not any(resolve_work_display_images(works, data_dir=tmp_path).values())
    directory = tmp_path / "artwork" / "work-0"
    directory.mkdir(parents=True)
    poster = directory / "poster.jpg"
    poster.write_bytes(b"fixture")
    assert (
        resolve_work_display_images(works, data_dir=tmp_path)["work-0"] == "/api/works/work-0/artwork/poster"
    )
    poster.unlink()
    assert resolve_work_display_images(works, data_dir=tmp_path)["work-0"] is None


def test_actor_portrait_can_fall_back_to_artwork_beyond_preview_window(client: TestClient) -> None:
    runtime: Runtime = app.state.runtime
    with runtime.database.session() as session:
        session.execute(update(Work).where(Work.id != "work-29").values(artwork=[]))
    response = client.get("/api/actors")
    response.raise_for_status()
    actor = next(row for row in response.json() if row["id"] == "actor")
    assert actor["image_url"] is not None
    assert len(actor["works"]) == 8 and actor["work_count"] == 30


def test_related_query_omits_heavy_columns_and_has_bounded_queries(client: TestClient) -> None:
    statements: list[str] = []
    runtime: Runtime = app.state.runtime

    def capture(connection: object, cursor: object, statement: str, *args: object) -> None:
        statements.append(statement)

    event.listen(runtime.database.engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/works/work-0/related")
        response.raise_for_status()
    finally:
        event.remove(runtime.database.engine, "before_cursor_execute", capture)
    scans = [sql for sql in statements if "FROM works ORDER BY" in sql]
    assert len(scans) == 1
    assert "works.plot" not in scans[0] and "works.reviews" not in scans[0]
    assert len(statements) <= 6
    assert len(response.json()["by_actor"]) == 18


def test_ranking_index_does_not_fetch_all_remote_lists_on_a_cold_load(
    client: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("section navigation must not depend on remote full lists")

    monkeypatch.setattr(JavRankingIndexCache, "load_curated_list", forbidden)
    app.state.runtime.response_cache.clear()
    response = client.get("/api/javranking/sections")
    response.raise_for_status()
    assert any(row["slug"] == "most-awarded-videos" for row in response.json()["sections"])


def test_stale_local_ranking_is_immediate_and_code_lookups_are_batched(
    client: TestClient,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    cached = CuratedList(
        slug="most-awarded-videos",
        title="Fixture",
        kind="videos",
        locale="zh-hans",
        base_url="https://example.com",
        revision="fixture",
        fetched_at=1.0,
        source_format="fixture",
        videos=tuple(
            CuratedVideoEntry(
                position=i + 1,
                code=f"ABC-{i + 1:03d}",
                title=f"Film {i}",
                cover_url=f"https://example.com/{i}.jpg",
            )
            for i in range(30)
        ),
    )
    JavRankingIndexCache(tmp_path / "javranking").write_curated_list(cached)

    def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("per-entry lookup should be replaced with a batched lookup")

    monkeypatch.setattr(Repository, "find_work_by_code", forbidden)
    monkeypatch.setattr(JavRankingIndexCache, "load_curated_list", forbidden)
    runtime: Runtime = app.state.runtime
    statements: list[str] = []

    def capture(connection: object, cursor: object, sql: str, *args: object) -> None:
        statements.append(sql)

    event.listen(runtime.database.engine, "before_cursor_execute", capture)
    try:
        response = client.get("/api/javranking/lists/most-awarded-videos")
    finally:
        event.remove(runtime.database.engine, "before_cursor_execute", capture)
    response.raise_for_status()
    assert len(response.json()["items"]) == 30
    assert all(row["state"] == "catalog_only" for row in response.json()["items"])
    assert len(statements) <= 3


def test_batched_code_lookup_preserves_normalized_identity_precedence(client: TestClient) -> None:
    runtime: Runtime = app.state.runtime
    with runtime.database.session() as session:
        session.add(
            ExternalIdentity(
                work_id="work-1",
                provider="global",
                kind="code",
                value="ABC-001",
                normalized_value=normalize_identity_value("ABC-001"),
            )
        )
    with runtime.database.session() as session:
        repo = Repository(session)
        codes = ["ABC-001", "abc-001", "ABC-003", "missing"]
        expected = {code: work.id for code in codes if (work := repo.find_work_by_code(code)) is not None}
        assert {
            code: work.id for code, work in repo.find_works_by_codes(codes, lean=True).items()
        } == expected
        assert expected["ABC-001"] == "work-1"


def test_search_works_without_a_cache_store(client: TestClient, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(app.state.runtime.response_cache, "set_raw", lambda *args, **kwargs: None)
    response = client.get("/api/works/search?q=ABC-030")
    response.raise_for_status()
    assert response.json()["total"] >= 1
    assert response.json()["items"][0]["primary_code"] == "ABC-030"
