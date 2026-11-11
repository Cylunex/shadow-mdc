"""Work/source separation, lossless tag migration and derived runtime filters."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shadow_mdc.api import app
from shadow_mdc.db.models import CatalogClassificationTerm, Work, WorkClassification
from shadow_mdc.services.classification_tags import migrate_genres_to_tags
from shadow_mdc.services.media_server import MediaServerSettings


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as client:
        yield client


def test_genres_move_to_tags_without_changing_language_or_losing_source_markers(client: TestClient) -> None:
    runtime = app.state.runtime
    with runtime.database.session() as session:
        session.add(Work(id="a", title="A", tags=["Drama", "forum"], field_locks=["tags"]))
        session.flush()
        session.add_all(
            [
                WorkClassification(work_id="a", term_id=identity)
                for identity in (
                    "genres:drama",
                    "genres:comedy",
                    "languages:ja",
                    "subtitle_languages:zh",
                    "forms:short",
                )
            ]
        )
    migrate_genres_to_tags(runtime.database.engine, runtime.tag_dictionary)
    with runtime.database.session() as session:
        work = session.get(Work, "a")
        assert work is not None
        assert work.tags[0:2] == ["Drama", "forum"]
        assert len(work.tags) == 3  # Drama and its migrated term are one identity.
        assert work.field_locks == ["tags"]
        assert session.get(WorkClassification, ("a", "genres:drama")) is None
        assert session.get(WorkClassification, ("a", "subtitle_languages:zh")) is not None
    taxonomy = client.get("/api/catalog/works/a/taxonomy").json()
    assert taxonomy["languages"] == ["languages:ja"]
    assert taxonomy["forms"] == []
    assert "genres" not in taxonomy and "subtitle_languages" not in taxonomy
    assert client.put("/api/catalog/works/a/taxonomy", json={"subtitle_languages": []}).status_code == 422
    schema = client.get("/api/catalog/classifications").json()
    names = {dimension["id"]: dimension["name"] for dimension in schema["dimensions"]}
    assert names["languages"] == "语言" and names["contents"] == "内容分区"
    assert "genres" not in names and "subtitle_languages" not in names
    forms = next(d["values"] for d in schema["dimensions"] if d["id"] == "forms")
    assert not {term["id"] for term in forms}.intersection({"forms:short", "forms:feature"})
    genres = client.get("/api/catalog/tags").json()["terms"]
    assert next(term for term in genres if term["name"] == "喜剧")["group"] == "题材"
    # Installed mobile clients receive the unified grouped tags through their old facet key.
    choices = client.post("/api/catalog/facets", json={}).json()["genres"]
    assert (
        choices
        and client.post("/api/catalog/works/query", json={"genres": [choices[0]["id"]]}).json()["total"] == 1
    )
    assert client.put("/api/catalog/works/a/taxonomy", json={"forms": []}).status_code == 200
    with runtime.database.session() as session:
        assert session.get(WorkClassification, ("a", "forms:short")) is not None
    # Old OR genre clauses remain ANDed with the other tag and language clauses.
    selection = {"genres": ["genres:drama", "genres:comedy"], "tags": ["剧情"], "languages": ["languages:ja"]}
    assert client.post("/api/catalog/works/query", json=selection).json()["total"] == 1
    assert (
        client.post(
            "/api/catalog/works/query", json={"subtitle_languages": ["subtitle_languages:zh"]}
        ).json()["total"]
        == 1
    )
    assert (
        client.post("/api/catalog/works/query", json={"forms": ["forms:short", "forms:vr"]}).json()["total"]
        == 1
    )
    playlist = client.post(
        "/api/catalog/playlists",
        headers={"X-Catalog-Owner": "test"},
        json={"name": "旧题材条件", "filters": selection},
    ).json()
    assert playlist["work_ids"] == ["a"]
    with runtime.database.session() as session:
        work = session.get(Work, "a")
        assert work is not None
        work.tags = ["forum"]
    migrate_genres_to_tags(runtime.database.engine, runtime.tag_dictionary)
    assert (
        client.get("/api/catalog/playlists", headers={"X-Catalog-Owner": "test"}).json()[0]["work_ids"] == []
    )
    with runtime.database.session() as session:
        assert session.get(Work, "a").tags == ["forum"]  # type: ignore[union-attr]


def test_runtime_boundaries_facets_playlists_and_mobile_share_the_same_filter(client: TestClient) -> None:
    runtime = app.state.runtime
    durations = [None, 0, -1, 1, 1799, 1800, 3599, 3600, 7199, 7200]
    with runtime.database.session() as session:
        session.add_all(
            [
                Work(id=str(index), title=str(index), runtime_seconds=value, family="jav")
                for index, value in enumerate(durations)
            ]
        )
    for filters, expected in [
        ({"duration_max": 1800}, {"3", "4"}),
        ({"duration_min": 1800, "duration_max": 3600}, {"5", "6"}),
        ({"duration_min": 3600, "duration_max": 7200}, {"7", "8"}),
        ({"duration_min": 7200}, {"9"}),
        ({"unknown_duration": True}, {"0", "1", "2"}),
    ]:
        page = client.post("/api/catalog/works/query", json=filters)
        assert page.status_code == 200, page.text
        assert {work["id"] for work in page.json()["items"]} == expected
    facets = client.post("/api/catalog/facets", json={"duration_min": 7200}).json()
    assert {facet["id"]: facet["count"] for facet in facets["durations"]} == {
        "under30": 2,
        "30to60": 2,
        "60to120": 2,
        "over120": 1,
        "unknown": 3,
    }
    for invalid in (
        {"duration_min": 5, "duration_max": 5},
        {"duration_min": -1},
        {"unknown_duration": True, "duration_max": 1800},
    ):
        assert client.post("/api/catalog/works/query", json=invalid).status_code == 422
    filters = {"duration_min": 1800, "duration_max": 3600, "contents": ["contents:jav"]}
    playlist = client.post(
        "/api/catalog/playlists",
        headers={"X-Catalog-Owner": "test"},
        json={"name": "半小时到一小时", "filters": filters},
    ).json()
    assert set(playlist["work_ids"]) == {"5", "6"}
    runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_TEST"))
    mobile = client.post(
        "/api/media/v1/catalog/works/query", json=filters, headers={"Authorization": "Bearer REPLACE_TEST"}
    )
    assert mobile.status_code == 200 and mobile.json()["total"] == 2
    # Physical duration labels remain archived; no inferred work language is created.
    with runtime.database.session() as session:
        assert list(session.scalars(select(WorkClassification))) == []
        assert session.get(CatalogClassificationTerm, "forms:short") is not None


def test_ambiguous_aliases_preserve_existing_tags_and_migrate_the_explicit_name(client: TestClient) -> None:
    runtime = app.state.runtime
    before = runtime.tag_dictionary.load()
    with runtime.database.session() as session:
        session.add(Work(id="a", title="A", tags=["forum"], field_locks=["title"]))
        session.add(
            CatalogClassificationTerm(
                id="genres:custom-fixture",
                dimension="genres",
                name="新题材",
                normalized_name="新题材",
                aliases=["剧情", "喜剧"],
            )
        )
        session.flush()
        session.add(WorkClassification(work_id="a", term_id="genres:custom-fixture"))
    migrate_genres_to_tags(runtime.database.engine, runtime.tag_dictionary)
    with runtime.database.session() as session:
        work = session.get(Work, "a")
        assert work is not None and work.field_locks == ["title", "tags"]
        assert work.tags[0] == "forum" and len(work.tags) == 2
        assert work.field_sources["tags"] == "manual"
        assert session.get(WorkClassification, ("a", "genres:custom-fixture")) is None
    registry = runtime.tag_dictionary.registry()
    assert registry.resolve("新题材").id not in {registry.resolve("剧情").id, registry.resolve("喜剧").id}
    for value in ("剧情", "喜剧"):
        assert registry.resolve(value).id == before.registry().resolve(value).id
    migrated = runtime.tag_dictionary.load()
    migrate_genres_to_tags(runtime.database.engine, runtime.tag_dictionary)
    assert runtime.tag_dictionary.load() == migrated
