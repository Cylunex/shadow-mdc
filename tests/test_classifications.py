"""Classification migration, independent dimensions and stable term identities."""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from shadow_mdc.api import app
from shadow_mdc.db.models import CatalogClassificationTerm, Work, WorkClassification, WorkTaxonomy
from shadow_mdc.db.repository import Database
from shadow_mdc.services.media_server import MediaServerSettings


def test_legacy_arrays_and_playlist_rules_migrate_once(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'migration.db'}")
    database.initialize()
    with database.session() as session:
        session.add_all([Work(id="a", title="A"), Work(id="b", title="B")])
    with database.engine.begin() as connection:
        for column in ("genres", "forms", "regions"):
            connection.exec_driver_sql(f"ALTER TABLE work_taxonomy ADD COLUMN {column} JSON DEFAULT '[]'")
        connection.exec_driver_sql(
            "INSERT INTO work_taxonomy (work_id, release_year, genres, forms, regions) "
            "VALUES (?, ?, ?, ?, ?)",
            ("a", 2012, json.dumps(["剧情", "Drama", "Custom Genre"]), '["短片"]', '["JP", "Japan"]'),
        )
        connection.exec_driver_sql(
            "INSERT INTO catalog_playlists (id, owner, name, work_ids, revision, created_at) "
            "VALUES ('list', 'owner', 'List', '[]', 1, '2026-01-01')",
        )
        connection.exec_driver_sql(
            "INSERT INTO catalog_playlist_rules VALUES (?, ?)",
            (
                "list",
                json.dumps({"genres": ["剧情", "Custom Genre"], "regions": ["JP"], "q": "keep"}),
            ),
        )
    database.initialize()
    with database.session() as session:
        values = list(
            session.scalars(select(WorkClassification.term_id).where(WorkClassification.work_id == "a"))
        )
        assert {"genres:drama", "forms:short", "regions:jp"}.issubset(values)
        assert len(values) == 4
        custom = session.scalar(
            select(CatalogClassificationTerm).where(
                CatalogClassificationTerm.name == "Custom Genre",
            )
        )
        assert custom is not None
        custom_id = custom.id
        assert session.get(WorkTaxonomy, "a").release_year == 2012  # type: ignore[union-attr]
    with database.engine.connect() as connection:
        columns = {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(work_taxonomy)")}
        assert not columns.intersection({"genres", "forms", "regions"})
        rule = json.loads(
            connection.exec_driver_sql("SELECT filters FROM catalog_playlist_rules").scalar_one()
        )
        assert rule == {"genres": ["genres:drama", custom_id], "regions": ["regions:jp"], "q": "keep"}
    database.initialize()
    with database.session() as session:
        assert len(list(session.scalars(select(WorkClassification)))) == 4
        assert session.get(CatalogClassificationTerm, custom_id) is not None
    database.engine.dispose()


def test_invalid_migration_rolls_back_without_removing_source_columns(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'invalid.db'}")
    database.initialize()
    with database.session() as session:
        session.add(Work(id="a", title="A"))
    with database.engine.begin() as connection:
        connection.exec_driver_sql("ALTER TABLE work_taxonomy ADD COLUMN genres JSON DEFAULT '[]'")
        connection.exec_driver_sql("INSERT INTO work_taxonomy (work_id, genres) VALUES ('a', '{}')")
    with pytest.raises(ValueError, match="Invalid legacy classification"):
        database.initialize()
    with database.engine.connect() as connection:
        assert "genres" in {row[1] for row in connection.exec_driver_sql("PRAGMA table_info(work_taxonomy)")}
        assert connection.exec_driver_sql("SELECT genres FROM work_taxonomy").scalar_one() == "{}"
        assert connection.exec_driver_sql("SELECT count(*) FROM work_classifications").scalar_one() == 0
    database.engine.dispose()


def test_dimensions_filters_editing_unknowns_and_mobile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all(
                [
                    Work(id="a", title="A", family="jav"),
                    Work(id="b", title="B", family="western"),
                    Work(id="c", title="C"),
                ]
            )
        payload = {
            "types": ["types:movie"],
            "forms": ["forms:vr", "forms:compilation", "forms:vr"],
            "languages": ["languages:ja"],
            "regions": ["regions:us"],
            "release_year": 2018,
        }
        response = client.put("/api/catalog/works/a/taxonomy", json=payload)
        assert response.status_code == 200, response.text
        assert response.json()["contents"] == ["contents:jav"]
        assert len(response.json()["forms"]) == 2
        assert (
            client.put("/api/catalog/works/a/taxonomy", json={"languages": ["regions:jp"]}).status_code == 422
        )
        assert client.put("/api/catalog/works/a/taxonomy", json={"genres": ["剧情"]}).status_code == 422
        assert client.get("/api/catalog/works/missing/taxonomy").status_code == 404
        query = {
            "contents": ["contents:jav"],
            "types": ["types:movie"],
            "forms": ["forms:compilation", "forms:vr"],
            "languages": ["languages:ja"],
            "regions": ["regions:us"],
        }
        page = client.post("/api/catalog/works/query", json=query).json()
        assert page["total"] == 1 and page["items"][0]["id"] == "a"
        assert page["items"][0]["classification_refs"]["languages"] == [
            {"id": "languages:ja", "name": "日语"}
        ]
        assert (
            client.post("/api/catalog/works/query", json={"languages": ["languages:unknown"]}).json()["total"]
            == 2
        )
        assert client.post("/api/catalog/works/query", json={"regions": ["regions:jp"]}).json()["total"] == 0
        facets = client.post("/api/catalog/facets", json=query).json()
        assert next(value["count"] for value in facets["forms"] if value["id"] == "forms:vr") == 1
        schema = client.get("/api/catalog/classifications").json()
        assert {dimension["id"] for dimension in schema["dimensions"]} == {
            "contents",
            "types",
            "forms",
            "languages",
            "regions",
            "decades",
        }
        languages = next(
            dimension["values"] for dimension in schema["dimensions"] if dimension["id"] == "languages"
        )
        assert next(value["count"] for value in languages if value["id"] == "languages:unknown") == 2
        assert client.put("/api/catalog/works/a/taxonomy", json={"forms": []}).json()["languages"] == [
            "languages:ja"
        ]
        app.state.runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_TEST"))
        assert client.get("/api/media/v1/catalog/classifications").status_code == 401
        mobile = {"Authorization": "Bearer REPLACE_TEST"}
        assert client.get("/api/media/v1/catalog/classifications", headers=mobile).status_code == 200
        assert (
            client.put("/api/media/v1/catalog/works/a/taxonomy", headers=mobile, json=payload).status_code
            == 405
        )


def test_term_rename_and_dynamic_playlist_keep_stable_ids(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'rename.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add(Work(id="a", title="A", family="jav"))
        created = client.post(
            "/api/catalog/classifications/terms",
            json={"dimension": "forms", "name": "自定义题材", "aliases": ["Original Alias"]},
        )
        assert created.status_code == 201, created.text
        term = created.json()
        assert (
            client.post(
                "/api/catalog/classifications/terms", json={"dimension": "forms", "name": "自定义题材"}
            ).status_code
            == 409
        )
        assert (
            client.post(
                "/api/catalog/classifications/terms", json={"dimension": "bad", "name": "Bad"}
            ).status_code
            == 422
        )
        selection = {"forms": [term["id"]], "languages": ["languages:ja"]}
        assert client.put("/api/catalog/works/a/taxonomy", json=selection).status_code == 200
        playlist = client.post(
            "/api/catalog/playlists",
            headers={"X-Catalog-Owner": "test-owner"},
            json={"name": "分类片单", "filters": selection},
        ).json()
        assert playlist["work_ids"] == ["a"]
        renamed = client.put(
            "/api/catalog/classifications/terms/" + term["id"], json={"name": "新题材", "revision": 1}
        )
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["id"] == term["id"]
        assert {"自定义题材", "Original Alias"}.issubset(renamed.json()["aliases"])
        assert (
            client.put(
                "/api/catalog/classifications/terms/" + term["id"], json={"name": "旧编辑", "revision": 1}
            ).status_code
            == 409
        )
        assert client.get("/api/catalog/works/a/taxonomy").json()["classification_refs"]["forms"] == [
            {"id": term["id"], "name": "新题材"}
        ]
        assert client.post("/api/catalog/works/query", json=selection).json()["total"] == 1
    with TestClient(app) as client:
        assert client.get("/api/catalog/playlists", headers={"X-Catalog-Owner": "test-owner"}).json()[0][
            "work_ids"
        ] == ["a"]
        assert (
            client.get("/api/catalog/works/a/taxonomy").json()["classification_refs"]["forms"][0]["name"]
            == "新题材"
        )
