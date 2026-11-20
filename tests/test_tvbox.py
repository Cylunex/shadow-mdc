import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.services import tvbox, tvbox_http
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.tvbox_http import Download, TvBoxError


def test_import_and_play_without_relay(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_OPERATOR_TOKEN")
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'tvbox.db'}")
    categories = [{"type_id": 1, "type_name": "电视剧"}]
    calls: list[str] = []
    signature = "first"

    async def fetch(url: str, **options: object) -> Download:
        calls.append(url)
        path = urlsplit(url).path
        query = parse_qs(urlsplit(url).query)
        if path == "/tvbox.json":
            raw = (Path(__file__).parent / "fixtures" / "tvbox" / "sources.jsonc").read_bytes()
            return Download(url, "application/json", raw)
        if path == "/offline":
            raise TvBoxError("上游连接失败或超时")
        if path == "/stream":
            return Download(url, "", b"#EXTM3U\n#EXTINF:10,\nvideo.ts\n")
        if path == "/cms":
            rows: list[dict[str, object]] = [
                {"vod_id": "00/演示", "vod_name": "示例剧", "type_id": 1},
                {"vod_id": "hidden", "vod_name": "未分组", "type_id": 99},
            ]
            if "ids" in query:
                rows = [row for row in rows if row["vod_id"] == query["ids"][0]]
                for row in rows:
                    row["vod_content"] = "<p>示例剧简介</p>"
                    row["vod_play_from"] = "线路一$$$线路二" if signature == "first" else "线路二$$$线路一"
                    first = f"第1集$/stream?sig={signature}|User-Agent=TVBox#第2集$/stream?ep=2"
                    second = "第1集$/stream?backup=1"
                    row["vod_play_url"] = (
                        f"{first}$$${second}" if signature == "first" else f"{second}$$${first}"
                    )
            body = httpx.Response(200, json={"class": categories, "list": rows, "pagecount": "2"}).content
            return Download(url, "application/json", body)
        raise AssertionError(path)

    monkeypatch.setattr(tvbox, "download", fetch)
    monkeypatch.setattr(tvbox_http, "download", fetch)
    headers = {"Authorization": "Bearer REPLACE_MOBILE_TOKEN"}
    prefix = "/api/media/v1/tvbox"
    with TestClient(app) as client:
        app.state.runtime.media_server_store.save(
            MediaServerSettings(
                media_client_token="REPLACE_MOBILE_TOKEN",
            )
        )
        assert client.get(prefix + "/sites").status_code == 401
        submitted = client.post("/api/tvbox/imports", json={"url": "https://example.com/tvbox.json"})
        assert submitted.status_code == 202
        job_id = submitted.json()["id"]
        result = None
        for _ in range(50):
            result = client.get(f"/api/tvbox/imports/{job_id}").json()["result"]
            if result is not None:
                break
            time.sleep(0.02)
        assert result is not None
        assert result["skipped"] == 1
        assert sum(site["failure"] is not None for site in result["sites"]) == 1
        assert not any(url.endswith("spider.jar") for url in calls)
        sites = client.get(prefix + "/sites", headers=headers).json()["items"]
        assert len(sites) == 1
        skill_headers = {"Authorization": "Bearer REPLACE_OPERATOR_TOKEN", "X-Catalog-Owner": "test-owner"}
        receipt = client.post("/api/skill/v1/call", headers=skill_headers, json={
            "name": "tvbox.status", "arguments": {"job_id": job_id},
        }).json()
        assert receipt["ok"] and receipt["result"]["result"] == result
        site_id = sites[0]["id"]
        assert client.post(f"{prefix}/imports", headers=headers, json={}).status_code in (404, 405)
        path = f"{prefix}/sites/{site_id}"
        response = client.get(path + "/categories", headers=headers)
        assert response.json()["items"] == [{"id": "1", "name": "电视剧"}]
        page = client.get(path + "/items?category=1", headers=headers).json()
        assert page["next_page"] == 2
        [item] = page["items"]
        assert item["title"] == "示例剧"
        item_path = f"{path}/items/{item['id']}"
        detail = client.get(item_path, headers=headers).json()
        assert detail["overview"] == "示例剧简介"
        assert len(detail["episodes"]) == 3
        assert all("url" not in episode for episode in detail["episodes"])
        episode_id = detail["episodes"][0]["id"]
        signature = "updated"
        resolved = client.post(item_path + "/resolve", headers=headers, json={"episode_id": episode_id})
        assert resolved.json() == {
            "url": "https://example.com/stream?sig=updated",
            "mime_type": "application/vnd.apple.mpegurl",
            "headers": {"User-Agent": "TVBox"},
        }
        assert resolved.headers["Cache-Control"] == "private, no-store"
        # A changed or temporarily broken upstream leaves the active directory available.
        categories[:] = []
        refreshed = client.post(f"/api/tvbox/sites/{site_id}/refresh").json()
        assert refreshed["active_revision"] == sites[0]["active_revision"]
        assert refreshed["failure"]
        assert client.patch(f"/api/tvbox/sites/{site_id}", json={"enabled": False}).status_code == 200
        assert client.get(prefix + "/sites", headers=headers).json() == {"items": []}
        assert client.get(item_path, headers=headers).status_code == 404
    with TestClient(app) as client:
        assert len(client.get("/api/tvbox/sites").json()["items"]) == 2


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/tvbox.json",
        "https://localhost/secret",
        "https://account:password@example.com/",
        "http://example.com/without-opt-in",
        "https://127.0.0.1/secret",
        "https://[::1]/secret",
    ],
)
async def test_rejects_invalid_sources(url: str) -> None:
    with pytest.raises(TvBoxError):
        await tvbox_http.download(url, allow_http=False)


def test_jsonc_keeps_quoted_content() -> None:
    assert tvbox.config_json('{"x": "https://example.com/a/*b*/", /* comment */ }') == {
        "x": "https://example.com/a/*b*/",
    }
    with pytest.raises(TvBoxError):
        tvbox.config_json('{"sites": [oops]}')
