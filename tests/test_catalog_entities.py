"""Entity boundaries, ownership, legacy migration and supported skill operations."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from typing import cast

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select

from shadow_mdc.api import app
from shadow_mdc.catalog_series import sync_field_series
from shadow_mdc.db.models import (
    Base,
    CatalogOrganization,
    CatalogSeries,
    CatalogWorkOrganization,
    CatalogWorkSeries,
    Collection,
    Work,
    WorkCollection,
)
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.services.catalog_export import export_catalog_bundle


def test_series_migration_retains_identity_dossier_members_and_explicit_ownership(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'legacy.db'}")
    Base.metadata.create_all(database.engine)
    with database.session() as session:
        session.add_all(
            [
                Collection(id="company", name="Company", normalized_name="company", kind="organization"),
                Collection(id="legacy", name="Cycle", normalized_name="cycle", kind="series"),
                Collection(
                    id="alias-collision", name="Old cycle", normalized_name="old cycle", kind="series"
                ),
                Collection(id="platform", name="Channel", normalized_name="channel", kind="platform"),
                Work(id="a", title="A", series="Cycle"),
                Work(id="b", title="B"),
                Work(id="c", title="C", series="Channel"),
            ]
        )
        session.flush()
        session.add_all(
            [
                CatalogOrganization(
                    id="legacy", aliases=["Old cycle"], description="Keep me", parent_id="company", revision=4
                ),
                WorkCollection(work_id="a", collection_id="legacy"),
                WorkCollection(work_id="b", collection_id="legacy"),
                WorkCollection(work_id="c", collection_id="platform"),
                CatalogWorkOrganization(work_id="b", organization_id="company", role="series"),
            ]
        )
    database.initialize()
    database.initialize()
    with database.session() as session:
        series = session.get(CatalogSeries, "legacy")
        assert series is not None
        assert (series.name, series.aliases, series.description, series.organization_id, series.revision) == (
            "Cycle",
            ["Old cycle"],
            "Keep me",
            "company",
            4,
        )
        assert session.scalar(select(func.count()).select_from(CatalogSeries)) == 4
        assert session.get(CatalogSeries, "alias-collision") is not None
        assert set(
            session.scalars(
                select(CatalogWorkSeries.work_id).where(
                    CatalogWorkSeries.series_id == "legacy",
                )
            )
        ) == {"a", "b"}
        assert session.get(Collection, "platform") is not None
        assert session.get(WorkCollection, ("c", "platform")) is None
        assert session.scalar(select(CatalogSeries).where(CatalogSeries.name == "Channel")) is not None
        assert session.scalar(select(CatalogSeries).where(CatalogSeries.name == "Company")) is not None


def test_field_membership_changes_keep_manual_membership(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'field.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="a",
                title="A",
                studio="Same",
                series="Same",
            ),
            overwrite=True,
        )
        first = session.scalar(select(CatalogSeries).where(CatalogSeries.name == "Same"))
        assert first is not None
        assert {row.kind for row in repo.collections_for_work(work.id)} == {"organization", "series"}
        work.series = "Next"
        sync_field_series(session, work)
        assert session.get(WorkCollection, (work.id, first.id)) is None
        next_series = session.scalar(select(CatalogSeries).where(CatalogSeries.name == "Next"))
        assert next_series is not None
        session.add(CatalogWorkSeries(work_id=work.id, series_id=next_series.id, source="manual"))
        session.flush()
        work.series = None
        sync_field_series(session, work)
        assert session.get(WorkCollection, (work.id, next_series.id)) is not None
        assert session.get(CatalogWorkSeries, (work.id, next_series.id, "manual")) is not None


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'entities.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST_TOKEN")
    with TestClient(app) as connected:
        yield connected


def test_series_are_not_organizations_and_hierarchy_and_filters_compose(client: TestClient) -> None:
    def organization(name: str, kind: str = "organization", parent: str | None = None) -> str:
        response = client.post(
            "/api/catalog/organizations",
            json={
                "name": name,
                "kind": kind,
                "parent_id": parent,
            },
        )
        assert response.status_code == 201, response.text
        return str(response.json()["id"])

    company = organization("Company")
    studio = organization("Studio", "studio", company)
    label = organization("Brand", "label", company)
    platform = organization("Channel", "platform")
    created = client.post("/api/catalog/series", json={"name": "Studio", "organization_id": label})
    assert created.status_code == 201, created.text
    series = created.json()["id"]
    assert series != studio
    assert (
        client.post("/api/catalog/organizations", json={"name": "Wrong", "kind": "series"}).status_code == 422
    )
    assert (
        client.post("/api/catalog/organizations", json={"name": "Wrong", "parent_id": series}).status_code
        == 404
    )
    assert (
        client.post("/api/catalog/organizations", json={"name": "Wrong", "parent_id": label}).status_code
        == 422
    )
    assert (
        client.post("/api/catalog/series", json={"name": "Wrong", "organization_id": series}).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/catalog/organizations/{company}",
            json={
                "name": "Company",
                "parent_id": studio,
                "revision": 1,
            },
        ).status_code
        == 409
    )
    with app.state.runtime.database.session() as session:
        repo = Repository(session)
        for identity, maker, brand, cycle in (
            ("a", "Studio", "Brand", "Studio"),
            ("b", "Other", None, "Studio"),
            ("c", "Studio", None, "Other cycle"),
        ):
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="fixture",
                    external_id=identity,
                    title=identity,
                    studio=maker,
                    label=brand,
                    series=cycle,
                    actors=("Actor",),
                ),
                overwrite=True,
            )
            if identity == "a":
                first_work = work.id
    assert (
        client.put(
            f"/api/catalog/organizations/{platform}/works",
            json={
                "work_id": first_work,
                "role": "platform",
            },
        ).status_code
        == 200
    )
    assert (
        client.put(
            f"/api/catalog/organizations/{series}/works",
            json={
                "work_id": first_work,
                "role": "studio",
            },
        ).status_code
        == 404
    )
    assert (
        client.put(
            f"/api/catalog/organizations/{company}/works",
            json={
                "work_id": first_work,
                "role": "series",
            },
        ).status_code
        == 422
    )
    assert series not in {row["id"] for row in client.get("/api/catalog/organizations").json()}
    facets = client.post("/api/catalog/facets", json={}).json()
    assert {row["role"] for row in facets["organizations"]} <= {"studio", "organization"}
    assert next(row for row in facets["organizations"] if row["id"] == company)["count"] == 2
    assert next(row for row in facets["series"] if row["id"] == series)["count"] == 2
    assert [row["id"] for row in facets["labels"]] == [label]
    assert [row["id"] for row in facets["platforms"]] == [platform]
    query = client.post(
        "/api/catalog/works/query",
        json={
            "organizations": [company],
            "labels": [label],
            "platforms": [platform],
            "series": [series],
        },
    )
    assert query.status_code == 200, query.text
    assert [row["title"] for row in query.json()["items"]] == ["a"]
    assert client.post("/api/catalog/works/query", json={"series": [company]}).json()["total"] == 0
    # Legacy saved selections still resolve through the series membership adapter.
    assert client.post("/api/catalog/works/query", json={"organizations": [series]}).json()["total"] == 2
    renamed = client.put(
        f"/api/catalog/series/{series}",
        json={
            "name": "Renamed cycle",
            "organization_id": label,
            "revision": 1,
        },
    )
    assert renamed.status_code == 200, renamed.text
    assert renamed.json()["aliases"] == ["Studio"]
    assert renamed.json()["work_count"] == 2
    original_organizations = client.get("/api/catalog/organizations").json()
    for identity, name in ((studio, "Renamed studio"), (label, "Renamed brand")):
        updated = client.put(
            f"/api/catalog/organizations/{identity}",
            json={
                "name": name,
                "revision": 1,
                "parent_id": company,
            },
        )
        assert updated.status_code == 200, updated.text
    client.post("/api/collections/seed")
    assert len(client.get("/api/catalog/series").json()) == 2
    assert len(client.get("/api/catalog/organizations").json()) == len(original_organizations)
    assert client.post("/api/catalog/works/query", json={"collection": "series:Studio"}).json()["total"] == 2
    assert client.post("/api/catalog/works/query", json={"q": "Renamed cycle"}).json()["total"] == 2
    assert (
        client.put(f"/api/catalog/series/{series}", json={"name": "Stale", "revision": 1}).status_code == 409
    )
    person = client.get("/api/catalog/people").json()[0]
    profile = client.get("/api/catalog/people/" + person["id"]).json()
    assert series not in {row["id"] for row in profile["organizations"]}
    assert series in {row["id"] for row in profile["series"]}


def test_skill_exposes_entity_operations_with_separate_schemas(client: TestClient) -> None:
    client.headers.update({"Authorization": "Bearer REPLACE_TEST_TOKEN", "X-Catalog-Owner": "fixture"})
    with app.state.runtime.database.session() as session:
        session.add(Work(id="work", title="Fixture"))

    def call(name: str, arguments: dict[str, object]) -> dict[str, object]:
        response = client.post("/api/skill/v1/call", json={"name": name, "arguments": arguments})
        assert response.status_code == 200, response.text
        result = response.json()
        assert result["ok"], result
        return cast(dict[str, object], result)

    company = call("organizations.create", {"name": "Company"})["result"]
    assert isinstance(company, dict)
    series = call("series.create", {"name": "Cycle", "organization_id": company["id"]})["result"]
    assert isinstance(series, dict)
    call("series.add", {"series_id": series["id"], "work_id": "work"})
    call("organizations.link", {"organization_id": company["id"], "work_id": "work", "role": "distributor"})
    call("organizations.update", {"organization_id": company["id"], "name": "Company", "revision": 1})
    call("series.update", {"series_id": series["id"], "name": "Cycle", "revision": 1})
    organizations = call("organizations.list", {})["result"]
    series_list = call("series.list", {})["result"]
    assert isinstance(organizations, list) and len(organizations) == 1
    assert isinstance(series_list, list) and len(series_list) == 1
    result = call("works.query", {"series": [series["id"]], "organizations": [company["id"]]})["result"]
    assert isinstance(result, dict) and result["total"] == 1


def test_empty_incremental_export_keeps_independent_series_and_its_owner(
    client: TestClient,
    tmp_path: Path,
) -> None:
    company = client.post("/api/catalog/organizations", json={"name": "Company"}).json()["id"]
    series = client.post(
        "/api/catalog/series",
        json={
            "name": "Cycle",
            "organization_id": company,
        },
    ).json()["id"]
    with app.state.runtime.database.session() as session:
        session.add(Work(id="work", title="Fixture"))
    assert client.put(f"/api/catalog/series/{series}/works", json={"work_id": "work"}).status_code == 200
    database = Path(str(app.state.runtime.database.engine.url.database))
    for incremental in (False, True):
        output = tmp_path / ("delta" if incremental else "full")
        export_catalog_bundle(
            source_data_dir=tmp_path,
            source_database=database,
            output=output,
            target_data_dir=PurePosixPath("/data"),
            incremental=incremental,
        )
    with sqlite3.connect(tmp_path / "delta/data/shadow-mdc.db") as connection:
        assert connection.execute("SELECT count(*) FROM works").fetchone()[0] == 0
        assert connection.execute(
            "SELECT organization_id FROM catalog_series WHERE id = ?", (series,)
        ).fetchone() == (company,)
        assert connection.execute("SELECT id FROM collections WHERE id = ?", (company,)).fetchone() == (
            company,
        )
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
