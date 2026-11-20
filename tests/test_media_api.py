"""Mobile paging and persistent playback links use isolated stores and fake Emby."""

from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import (
    Actor,
    CatalogSeries,
    CatalogWorkSeries,
    Collection,
    Library,
    MediaAsset,
    Work,
    WorkActor,
    WorkCollection,
)
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.playback import PlayResolveResult, PlaySource


def test_mobile_contract(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'test.db'}")
    searches: list[str] = []

    def emby(request: httpx.Request) -> httpx.Response:
        assert request.headers["X-Emby-Token"] == "REPLACE_SERVER_TOKEN"
        if request.url.path.endswith("System/Info/Public"):
            return httpx.Response(200, json={"Id": "server-a"})
        assert request.url.path.endswith("Items"), "Emby exact lookup uses /Items?Ids=..."
        if request.url.params.get("Ids"):
            item_id = request.url.params["Ids"]
            rows = [] if item_id == "missing" else [{
                "Id": "item-a" if item_id == "wrong-id" else item_id,
                "Type": "Folder" if item_id == "folder" else "Movie",
            }]
            assert "SearchTerm" not in request.url.params
            return httpx.Response(200, json={"Items": rows, "TotalRecordCount": len(rows)})
        searches.append(str(request.url))
        return httpx.Response(200, json={"Items": [
            {"Id": "item-a", "Name": "ABC-123", "Type": "Movie"},
            {"Id": "wrong", "Name": "ABC-1234", "Type": "Movie"},
        ], "TotalRecordCount": 2})

    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.media_server_store.save(MediaServerSettings(
            enabled=True, base_url="https://example.com", api_key="REPLACE_SERVER_TOKEN",
            media_client_token="REPLACE_MOBILE_TOKEN",
        ))
        async def get(
            url: str, *, headers: dict[str, str], params: dict[str, str] | None, timeout: int,
        ) -> httpx.Response:
            request = httpx.Request("GET", url, headers=headers, params=params)
            response = emby(request)
            response.request = request
            return response

        monkeypatch.setattr(runtime.http, "get", get)
        with runtime.database.session() as session:
            session.add_all([Work(id="a", title="First", primary_code="ABC-123", actors=["An Actor"]),
                             Work(id="b", title="Second", primary_code="ABC-124"),
                             Work(id="c", title="No code")])
        assert client.get("/api/media/v1/works").status_code == 401
        client.headers["Authorization"] = "Bearer REPLACE_MOBILE_TOKEN"
        first = client.get("/api/media/v1/works", params={"limit": 1}).json()
        second = client.get("/api/media/v1/works", params={
            "limit": 1, "cursor": first["next_cursor"],
        }).json()
        assert first["items"][0]["id"] != second["items"][0]["id"]
        assert client.get("/api/media/v1/works", params={
            "q": "changed", "cursor": first["next_cursor"],
        }).status_code == 400
        assert client.get("/api/media/v1/works", params={"q": "Actor"}).json()["items"][0]["id"] == "a"
        assert client.post("/api/media/v1/works/a/resolve").json() == {
            "server_id": "server-a", "item_id": "item-a",
        }
        assert len(searches) == 1
        assert client.post("/api/media/v1/works/a/resolve").status_code == 200
        assert len(searches) == 1
        assert client.post("/api/media/v1/works/c/resolve").status_code == 409
        assert client.put("/api/media/v1/works/c/binding", json={"item_id": "../bad"}).status_code == 400
        assert client.put("/api/media/v1/works/c/binding", json={"item_id": "missing"}).status_code == 404
        assert client.put("/api/media/v1/works/c/binding", json={"item_id": "wrong-id"}).status_code == 404
        assert client.put("/api/media/v1/works/c/binding", json={"item_id": "folder"}).status_code == 409
        assert client.put("/api/media/v1/works/c/binding", json={"item_id": "item-a"}).status_code == 200
        assert client.post("/api/media/v1/works/c/resolve").json()["item_id"] == "item-a"
        # Editing the existing Web settings must retain the file-only mobile secret.
        client.put("/api/settings/media-server", json={"enabled": False})
        assert runtime.media_server_store.load().media_client_token == "REPLACE_MOBILE_TOKEN"


def test_mobile_work_filters_and_authenticated_sidecar(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'mobile.db'}")
    root = tmp_path / "library"
    root.mkdir()
    (root / "Movie.mkv").touch()
    (root / "Movie.zh.srt").write_text("1\n00:00:00,000 --> 00:00:02,000\nHello\n")
    (root / "Movie.en.srt").symlink_to(tmp_path / "private")
    (tmp_path / "private").write_text("private")
    source = PlaySource(kind="direct_video", label="Online", url="https://example.com/movie.mp4",
                        playable_in_app=True)
    monkeypatch.setattr("shadow_mdc.media_api.resolve_play", AsyncMock(return_value=PlayResolveResult(
        work_id="a", sources=[source])))
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_MOBILE_TOKEN"))
        with runtime.database.session() as session:
            session.add_all([Work(id="a", title="Movie"), Work(id="b", title="Other"),
                             Library(id="lib", name="Library", root_path=str(root)),
                             Collection(id="label", name="Label", normalized_name="label", kind="label")])
            session.flush()
            session.add(WorkCollection(work_id="a", collection_id="label"))
            session.add(MediaAsset(library_id="lib", work_id="a", path=str(root / "Movie.mkv"),
                                   size=0, hints={}))
        headers = {"Authorization": "Bearer REPLACE_MOBILE_TOKEN"}
        prefix = "/api/media/v1/works"
        assert client.get("/api/media/v1/catalog/works/a/filters", headers=headers).json() == [
            {"dimension": "label", "id": "label", "name": "Label"}]
        plan = client.post(f"{prefix}/a/plan?online=true", headers=headers).json()
        [track] = plan["subtitles"]
        assert track["label"] == "Movie.zh.srt" and str(root) not in str(plan)
        url = track["url"]
        assert client.get(url).status_code == 401
        assert client.get(url.replace("/works/a/", "/works/b/"), headers=headers).status_code == 404
        response = client.get(url, headers=headers)
        assert response.text.startswith("WEBVTT") and "Hello" in response.text
        assert response.headers["content-type"].startswith("text/vtt")
        (root / "Movie.zh.srt").unlink()
        assert client.get(url, headers=headers).status_code == 404


def test_public_search_matches_the_catalog_on_all_clients(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'search.db'}")
    mobile = "/api/media/v1"

    def expect(client: TestClient, query: str, expected: list[str]) -> None:
        responses = [
            client.get(mobile + "/works", params={"q": query}),
            client.post(mobile + "/catalog/works/query", json={"q": query}),
            client.post("/api/catalog/works/query", json={"q": query}),
        ]
        for response in responses:
            assert response.status_code == 200, response.text
            assert [item["id"] for item in response.json()["items"]] == expected, query

    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_MOBILE_TOKEN"))
        client.headers["Authorization"] = "Bearer REPLACE_MOBILE_TOKEN"
        with runtime.database.session() as session:
            session.add_all([
                Work(id="film", title="Display title", original_title="原版片名",
                     actors=["旧署名", "客串_甲%"]),
                Work(id="second", title="Different", actors=["客串乙"]),
                Actor(id="actor", name="旧署名", normalized_name="旧署名"),
                Collection(id="company", name="Studio", normalized_name="studio", kind="studio",
                           aliases=["制作旧称"]),
                CatalogSeries(id="series", name="Series", normalized_name="series", aliases=["早期系列名"]),
            ])
            session.flush()
            session.add_all([
                WorkActor(work_id="film", actor_id="actor"),
                WorkCollection(work_id="film", collection_id="company"),
                CatalogWorkSeries(work_id="film", series_id="series"),
            ])

        changed = client.put("/api/catalog/people/actor", json={
            "name": "新艺名", "aliases": ["\uff21\uff2b\uff29"],  # Fullwidth AKI.
            "notes": "私有标记", "match_names": ["目录提示"], "revision": 1,
        })
        assert changed.status_code == 200, changed.text
        for query in ("display", "原版片", "新艺名", "旧署名", "aki", "客串_", "甲%", "制作旧称", "早期系列"):
            expect(client, query, ["film"])
        for query in ("目录提示", "私有标记", "客串%"):
            expect(client, query, [])
        by_credit = client.post("/api/catalog/works/query", json={"q": "制作旧称", "actor_name": "新艺名"})
        assert by_credit.json()["total"] == 1
        mismatched = client.post("/api/catalog/works/query", json={"q": "客串乙", "actor_name": "新艺名"})
        assert mismatched.json()["total"] == 0
        facets = client.post("/api/catalog/facets", json={"q": "aki"}).json()
        assert [(group["id"], group["count"]) for group in facets["organizations"]] == [("company", 1)]

        detach = client.put("/api/catalog/people/actor/works", json={
            "work_id": "film", "included": False, "revision": changed.json()["revision"],
        })
        assert detach.status_code == 200, detach.text
        for query in ("新艺名", "旧署名", "aki"):
            expect(client, query, [])
        expect(client, "客串_", ["film"])

        # Simulate upgrading a library with a warm actor index and an existing exclusion.
        with runtime.database.engine.begin() as connection:
            connection.exec_driver_sql("DROP TABLE mdc_people_excluded_names")

    with TestClient(app) as client:
        client.headers["Authorization"] = "Bearer REPLACE_MOBILE_TOKEN"
        expect(client, "旧署名", [])
        attach = client.put("/api/catalog/people/actor/works", json={
            "work_id": "second", "revision": detach.json()["revision"],
        })
        assert attach.status_code == 200, attach.text
        expect(client, "新艺名", ["second"])
        expect(client, "旧署名", ["second"])
