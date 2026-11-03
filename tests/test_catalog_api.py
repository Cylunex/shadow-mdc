from datetime import date
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Actor, Collection, Work, WorkActor, WorkCollection
from shadow_mdc.services.playback import VodProviderConfig, search_vod_provider


def test_unified_catalog_facets_and_actor_intersection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="a", title="One", release_date=date(2015, 1, 1),
                     actors=["A", "B"], tags=["海滩", "剧情"]),
                Work(id="b", title="Two", release_date=date(2020, 1, 1), actors=["A"], tags=["海滩"]),
                Work(id="c", title="Unknown year", actors=[], tags=[]),
                Actor(id="aa", name="A", normalized_name="a"),
                Actor(id="bb", name="B", normalized_name="b"),
                Collection(id="studio", name="Studio", normalized_name="studio", kind="studio"),
            ])
            session.flush()
            session.add_all([
                WorkActor(work_id="a", actor_id="aa"), WorkActor(work_id="a", actor_id="bb"),
                WorkActor(work_id="b", actor_id="aa"),
                WorkCollection(work_id="a", collection_id="studio"),
                WorkCollection(work_id="b", collection_id="studio"),
            ])
        result = client.post("/api/catalog/works/query", json={
            "actors": ["aa", "bb"], "actors_all": True,
        })
        assert result.status_code == 200, result.text
        assert [w["id"] for w in result.json()["items"]] == ["a"]
        facets = client.post("/api/catalog/facets", json={"organizations": ["studio"]})
        assert facets.status_code == 200, facets.text
        assert facets.json()["organizations"][0]["count"] == 2
        assert {f["id"] for f in facets.json()["decades"]} == {"2010", "2020"}
        assert client.post("/api/catalog/works/query", json={"unknown_year": True}).json()["total"] == 1
        first = client.post("/api/catalog/works/query", json={"limit": 1}).json()
        assert client.post("/api/catalog/works/query", json={
            "cursor": first["next_cursor"], "q": "changed",
        }).status_code == 400
        assert client.get("/api/media/v1/catalog/people").status_code == 503
        assert client.put("/api/catalog/works/c/taxonomy", json={
            "genres": ["纪录"], "forms": ["短片"], "regions": ["JP"], "release_year": 2012,
        }).status_code == 200
        selected = client.post("/api/catalog/works/query", json={"genres": ["纪录"], "year_from": 2010})
        assert selected.json()["items"][0]["id"] == "c"
        assert selected.json()["items"][0]["year"] == 2012
        assert client.post("/api/catalog/works/query", json={"unknown_year": True}).json()["total"] == 0
        assert client.post("/api/catalog/facets", json={}).json()["genres"][0]["name"] == "纪录"
        people = client.get("/api/catalog/people").json()
        assert next(p for p in people if p["id"] == "aa")["work_ids"] == ["a", "b"]


@pytest.mark.asyncio
async def test_online_candidates_require_exact_identity_and_isolate_parts() -> None:
    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"list": [
            {"vod_id": 1, "type_id": 1, "vod_name": "ABC-1234",
             "vod_play_url": "https://example.com/wrong.m3u8"},
            {"vod_id": 2, "type_id": 1, "vod_name": "ABC-123",
             "vod_play_url": "CD1$https://example.com/one.m3u8#CD2$https://example.com/two.m3u8"},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        sources = await search_vod_provider(VodProviderConfig(
            name="fixture", base_url="https://example.com/api", whitelist_type_ids=[1],
        ), client, code="ABC-123")
    assert len(sources) == 2
    assert sources[0].version_id != sources[1].version_id
    assert all("wrong" not in source.url for source in sources)
