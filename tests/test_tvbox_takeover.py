import hashlib
import io
import json
import logging
import re
import tarfile
import time
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import TvBoxRevision, TvBoxSite
from shadow_mdc.services import tvbox, tvbox_http, tvbox_stream
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.tvbox_http import Download, TvBoxError
from shadow_mdc.services.tvbox_publish import TvBoxFeedLogFilter
from shadow_mdc.services.tvbox_xml import normalize_xml


@pytest.fixture
def mdc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'tvbox.db'}")
    with TestClient(app) as client:
        yield client


def wait_for_job(client: TestClient, job: httpx.Response) -> dict[str, object]:
    assert job.status_code == 202, job.text
    for _ in range(50):
        response = client.get("/api/tvbox/imports/" + job.json()["id"])
        result = response.json()["result"]
        if result is not None:
            assert isinstance(result, dict)
            return result
        time.sleep(0.02)
    raise AssertionError("import did not finish")


def test_browser_hls_and_range_are_scoped_to_a_resolved_episode(
    mdc: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with app.state.runtime.database.session() as session:
        session.add(
            TvBoxSite(id="example", name="演示", endpoint="https://example.com/cms", active_revision="rev")
        )
        session.flush()
        session.add(TvBoxRevision(id="rev", site_id="example", categories=[{"id": "1", "name": "电影"}]))

    async def catalog(url: str, **options: object) -> Download:
        body = {
            "list": [
                {
                    "vod_id": "1",
                    "vod_name": "演示",
                    "type_id": 1,
                    "vod_play_from": "线路一",
                    "vod_play_url": "第1集$https://example.com/media/main|Referer=https%3A%2F%2Fexample.com%2F",
                }
            ]
        }
        return Download(url, "application/json", json.dumps(body).encode())

    async def check(url: str, **options: object) -> str:
        return "application/vnd.apple.mpegurl"

    @asynccontextmanager
    async def open_stream(
        url: str, *, allow_http: bool, headers: dict[str, str]
    ) -> AsyncIterator[tuple[httpx.URL, httpx.Response]]:
        assert headers.get("Referer") == "https://example.com/"
        path = urlsplit(url).path
        mime = {"content-type": "application/octet-stream", "set-cookie": "private=secret"}
        status = 200
        if path == "/media/main":
            body = b"#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=80000\nvariant\n"
        elif path == "/media/variant":
            body = b'#EXTM3U\n#EXT-X-KEY:METHOD=AES-128,URI="key"\n#EXTINF:3,\nmedia.ts\n'
        elif path == "/media/media.ts":
            assert headers.get("Range") == "bytes=0-2"
            body, status = b"abc", 206
            mime["content-range"] = "bytes 0-2/9"
        elif path == "/media/key":
            body = b"0123456789abcdef"
        else:
            raise AssertionError(path)
        yield (
            httpx.URL(url),
            httpx.Response(
                status, headers=mime, stream=httpx.ByteStream(body), request=httpx.Request("GET", url)
            ),
        )

    monkeypatch.setattr(tvbox, "download", catalog)
    monkeypatch.setattr(tvbox, "playback_mime", check)
    monkeypatch.setattr(tvbox_stream, "open_remote", open_stream)
    item = "/api/tvbox/sites/example/items/MQ"
    episode = mdc.get(item).json()["episodes"][0]["id"]
    issued = mdc.post(item + "/watch", json={"episode_id": episode}).json()
    assert issued["url"].startswith("/api/tvbox/stream/")
    assert issued["headers"] == {}
    root = issued["url"].rsplit("/", 1)[0]
    master = mdc.get(issued["url"])
    assert "example.com" not in master.text
    variant = mdc.get(root + "/" + master.text.splitlines()[-1])
    assert "example.com" not in variant.text
    key = re.search(r'URI="([^"]+)"', variant.text)
    assert key is not None and mdc.get(root + "/" + key[1]).content == b"0123456789abcdef"
    segment = root + "/" + variant.text.splitlines()[-1]
    media = mdc.get(segment, headers={"Range": "bytes=0-2"})
    assert media.status_code == 206 and media.content == b"abc"
    assert media.headers["Content-Range"] == "bytes 0-2/9"
    assert "set-cookie" not in media.headers
    assert mdc.get(segment, headers={"Range": "bytes=0-2,5-9"}).status_code == 416
    assert mdc.get(root + "/made-up").status_code == 404
    mdc.app.state.runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_MOBILE_TOKEN"))
    auth = {"Authorization": "Bearer REPLACE_MOBILE_TOKEN"}
    mobile = mdc.post(item.replace("/api/tvbox", "/api/media/v1/tvbox") + "/watch",
                      headers=auth, json={"episode_id": episode}).raise_for_status().json()["url"]
    assert mdc.get(mobile).status_code == 401
    assert "#EXT-X-STREAM-INF" in mdc.get(mobile, headers=auth).raise_for_status().text
    assert mdc.get(mobile.replace("/api/media/v1/tvbox", "/api/tvbox")).status_code == 404
    crossed = issued["url"].replace("/api/tvbox", "/api/media/v1/tvbox")
    assert mdc.get(crossed, headers=auth).status_code == 404
    mdc.patch("/api/tvbox/sites/example", json={"enabled": False})
    assert mdc.get(issued["url"]).status_code == 404


def test_relay_archive_restores_type_zero_group_and_revocable_binding(
    mdc: TestClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def fetch(url: str, **options: object) -> Download:
        query = parse_qs(urlsplit(url).query)
        if url.endswith("/file"):
            return Download(url, "video/mp4", b"\0\0\0\x20ftypisom\0\0\0\0")
        assert query.get("ac") in (None, ["videolist"])
        return Download(url, "application/xml", XML)

    monkeypatch.setattr(tvbox, "download", fetch)
    monkeypatch.setattr(tvbox_http, "download", fetch)
    token = "test-token-abcdefghijklmnopqrstuvwxyz"
    pack = {"id": "source", "data": {"protocol": "tvbox", "enabled": True, "activeRevision": "revision"}}
    old_revision = {
        "id": "revision",
        "data": {
            "sourceId": "source",
            "status": "active",
            "normalized": {
                "items": [
                    {
                        "group": "site",
                        "name": "演示 XML",
                        "url": "https://example.com/xml",
                        "data": {"type": 0},
                    }
                ],
            },
        },
    }
    relay_manifest = {
        "format": "shadow-relay-data/v1",
        "tables": {
            "sources": [pack, {"id": "novel", "data": {"protocol": "legado-book"}}],
            "revisions": [old_revision, {"id": "private-novel", "data": {"sourceId": "novel"}}],
            "source_sets": [
                {
                    "id": "group",
                    "data": {
                        "name": "家庭影视",
                        "members": [
                            {"sourceId": "source"},
                            {"sourceId": "novel"},
                        ],
                    },
                }
            ],
            "bindings": [
                {
                    "id": "binding",
                    "data": {
                        "setId": "group",
                        "name": "电视",
                        "formats": ["tvbox/store.json"],
                        "tokenHash": hashlib.sha256(token.encode()).hexdigest(),
                    },
                }
            ],
        },
    }
    file = io.BytesIO()
    with tarfile.open(fileobj=file, mode="w:gz") as archive:
        content = json.dumps(relay_manifest).encode()
        member = tarfile.TarInfo("manifest.json")
        member.size = len(content)
        archive.addfile(member, io.BytesIO(content))
    imported = wait_for_job(
        mdc,
        mdc.post(
            "/api/tvbox/imports/relay",
            files={"file": ("relay.tar.gz", file.getvalue(), "application/gzip")},
        ),
    )
    migrated = imported["migrated"]
    assert isinstance(migrated, dict) and migrated["revisions"] == 1 and migrated["restored_bindings"] == 1
    [group] = mdc.get("/api/tvbox/groups").json()["items"]
    [site_id] = group["site_ids"]
    assert mdc.get(f"/api/tvbox/sites/{site_id}/items").json()["items"][0]["year"] == "2024"
    assert mdc.get("/api/tvbox/relay-history").json()[0]["id"] == "revision"

    path = f"/api/tvbox-feed/{token}/store.json"
    store = mdc.get(path)
    [entry] = store.json()["urls"]
    [site] = mdc.get(entry["url"]).json()["sites"]
    assert entry["name"] == "家庭影视" and site["type"] == 0
    assert mdc.get(path, headers={"If-None-Match": store.headers["ETag"]}).status_code == 304
    assert mdc.patch(f"/api/tvbox/sites/{site_id}", json={"name": "改名"}).status_code == 200
    assert mdc.get(entry["url"]).json()["sites"][0]["name"] == "演示 XML"
    assert mdc.get(path.replace("store.json", "sites.json")).json()["sites"][0]["name"] == "改名"
    assert mdc.patch(f"/api/tvbox/sites/{site_id}", json={"hidden": True}).status_code == 200
    assert mdc.get(entry["url"]).status_code == 404
    [binding] = mdc.get("/api/tvbox/subscriptions").json()
    response = mdc.post(f"/api/tvbox/subscriptions/{binding['id']}/rotate")
    assert response.status_code == 200
    assert mdc.get(path).status_code == 404
    assert mdc.get(response.json()["path"]).status_code == 200


XML = b"""<?xml version="1.0" encoding="utf-8"?>
<rss version="5.1"><list page="1" pagecount="1"><video>
<id>1</id><tid>1</tid><type>Movie</type><name>Example</name><year>2024</year>
<dl><dd flag="mp4"><![CDATA[Main$https://example.com/file]]></dd></dl>
</video></list><class><ty id="1">Movie</ty></class></rss>
"""


def test_untrusted_xml_rejects_dtd_and_invalid_catalog() -> None:
    with pytest.raises(TvBoxError, match="不支持的声明"):
        normalize_xml(b'<!DOCTYPE rss [<!ENTITY test "test">]><rss><list/></rss>')
    with pytest.raises(TvBoxError, match="CMS 目录"):
        normalize_xml(b"<html/>")


def test_feed_access_log_hides_subscription_token() -> None:
    record = logging.LogRecord(
        "uvicorn.access",
        logging.INFO,
        __file__,
        1,
        '%s - "%s %s HTTP/%s" %d',
        ("127.0.0.1", "GET", "/mdc/api/tvbox-feed/test-secret/store.json", "1.1", 200),
        None,
    )
    assert TvBoxFeedLogFilter().filter(record)
    assert "/mdc/api/tvbox-feed/[redacted]/store.json" in record.getMessage()
    assert "test-secret" not in record.getMessage()
