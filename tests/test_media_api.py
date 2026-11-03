"""Mobile paging and persistent playback links use isolated stores and fake Emby."""

from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Work
from shadow_mdc.services.media_server import MediaServerSettings


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
