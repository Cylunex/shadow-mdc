"""Unit tests for 115 Open Platform client + STRM writer (httpx mocked)."""

from __future__ import annotations

import base64
import hashlib
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pytest

from shadow_mdc.media.strm import read_strm_locator, write_strm
from shadow_mdc.services.pan import (
    Pan115Client,
    PanService,
    PanSettings,
    generate_pkce,
    map_remote_status,
    write_offline_strm,
)


def test_generate_pkce_shapes() -> None:
    verifier, challenge = generate_pkce()
    assert "=" not in verifier
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    assert challenge == base64.b64encode(digest).decode("ascii")


@pytest.mark.parametrize(
    ("status", "expected"),
    [(0, "running"), (1, "running"), (2, "done"), (-1, "failed"), (None, "running")],
)
def test_map_remote_status(status: int | None, expected: str) -> None:
    assert map_remote_status(status) == expected


def test_write_strm_roundtrip(tmp_path: Path) -> None:
    path = tmp_path / "CODE" / "CODE.strm"
    write_strm(path, "http://openlist:5244/d/115/CODE/CODE.mp4")
    assert read_strm_locator(path) == "http://openlist:5244/d/115/CODE/CODE.mp4"


def test_write_offline_strm_best_effort(tmp_path: Path) -> None:
    settings = PanSettings(
        strm_enabled=True,
        strm_output_root=str(tmp_path / "strm"),
        strm_url_prefix="http://openlist:5244/d/115",
    )
    path = write_offline_strm(
        settings=settings,
        work_code="SSIS-123",
        file_name="video.mp4",
        remote_relative="JAV/SSIS-123/SSIS-123.mp4",
    )
    assert path is not None
    assert Path(path).read_text(encoding="utf-8").strip() == (
        "http://openlist:5244/d/115/JAV/SSIS-123/SSIS-123.mp4"
    )


@pytest.mark.asyncio
async def test_pan_client_token_and_offline_flow(tmp_path: Path) -> None:
    calls: list[tuple[str, str]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append((request.method, request.url.path))
        path = request.url.path
        if path.endswith("/open/authDeviceCode"):
            return httpx.Response(
                200,
                json={"state": True, "code": 0, "data": {"uid": "u1", "time": 100, "sign": "s1"}},
            )
        if path.endswith("/qrcode"):
            # minimal PNG header
            return httpx.Response(200, content=b"\x89PNG\r\n\x1a\n" + b"\x00" * 16)
        if "/get/status" in path:
            return httpx.Response(200, json={"data": {"status": 2}})
        if path.endswith("/open/deviceCodeToToken"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": {
                        "access_token": "access-1",
                        "refresh_token": "refresh-1",
                        "expires_in": 3600,
                    },
                },
            )
        if path.endswith("/open/offline/add_task_urls"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": [{
                        "state": True,
                        "code": 0,
                        "info_hash": "ABC123",
                        "url": "magnet:?xt=urn:btih:ABC123",
                    }],
                },
            )
        if path.endswith("/open/offline/get_task_list"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": {
                        "page": 1,
                        "page_count": 1,
                        "count": 1,
                        "tasks": [
                            {
                                "info_hash": "ABC123",
                                "status": 2,
                                "percentDone": 100,
                                "file_id": "f1",
                                "name": "SSIS-123.mp4",
                                "wp_path_id": "42",
                            }
                        ],
                    },
                },
            )
        if path.endswith("/open/ufile/files"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "count": 1,
                    "cid": 0,
                    "data": [{"fid": "d1", "fn": "云下载", "fc": "0", "pid": "0"}],
                },
            )
        if path.endswith("/open/user/info"):
            return httpx.Response(
                200,
                json={"state": True, "code": 0, "data": {"user_id": "9", "user_name": "tester"}},
            )
        if path.endswith("/open/refreshToken"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": {
                        "access_token": "access-2",
                        "refresh_token": "refresh-2",
                        "expires_in": 3600,
                    },
                },
            )
        return httpx.Response(404, json={"message": f"unhandled {path}"})

    transport = httpx.MockTransport(handler)
    client = Pan115Client(client_id="100197303")
    client._http = httpx.AsyncClient(transport=transport)

    verifier, uid, time_value, sign, png = await client.begin_device_login()
    assert uid == "u1" and time_value == 100 and sign == "s1"
    assert png.startswith(b"\x89PNG")
    assert await client.poll_login_status(uid=uid, time_value=time_value, sign=sign) == "ok"
    creds = await client.exchange_token(uid=uid, code_verifier=verifier)
    assert creds.access_token == "access-1"
    client.bind_tokens(creds)

    submitted = await client.enqueue_remote_urls(
        ["magnet:?xt=urn:btih:ABC123"], directory_id="42"
    )
    assert submitted[0]["info_hash"] == "ABC123"
    listing = await client.get_task_list(page=1)
    assert listing["tasks"][0]["local_status"] == "done"
    files = await client.list_directory("0", page=1)
    assert files["items"][0]["name"] == "云下载"
    assert files["items"][0]["is_directory"] is True

    await client.aclose()


def test_pan_service_imports_credentials_without_echoing_tokens(tmp_path: Path) -> None:
    service = PanService(data_dir=tmp_path, client_id="100197303")
    status = service.import_tokens(
        "access-token-fixture",
        "refresh-token-fixture",
        expires_in=86400,
        user_name="u",
    )
    cred_path = tmp_path / "pan" / "credentials.json"
    assert cred_path.is_file()
    raw = json.loads(cred_path.read_text(encoding="utf-8"))
    assert raw["access_token"] == "access-token-fixture"
    assert raw["refresh_token"] == "refresh-token-fixture"
    assert status["configured"] is True
    assert status["available"] is True
    assert status["connected"] is True
    assert "access_token" not in status
    assert "refresh_token" not in status
    assert service.get_client()._access_token == "access-token-fixture"
    service.disconnect()
    assert service.status()["configured"] is False


def test_pan_service_switching_client_id_clears_credentials(tmp_path: Path) -> None:
    service = PanService(data_dir=tmp_path, client_id="100197303")
    service.import_tokens("a", "r")
    saved = service.save_settings({"client_id": "own-app-id", "client_secret": "own-secret"})
    assert saved.client_id == "own-app-id"
    assert service.status()["configured"] is False
    assert service.settings_status()["client_secret_set"] is True


def test_parse_retry_after_seconds_and_empty() -> None:
    from shadow_mdc.services.pan import parse_retry_after

    assert parse_retry_after(None) == 0.0
    assert parse_retry_after("") == 0.0
    assert parse_retry_after("2") == 2.0
    assert parse_retry_after("0") == 0.0


def _bind_token(client: Pan115Client) -> None:
    client._access_token = "t"
    client._refresh_token = "r"
    client._expires_at = datetime.now(UTC) + timedelta(hours=1)


@pytest.mark.asyncio
async def test_pan_client_honors_retry_after_and_caps_inflight() -> None:
    import asyncio

    from shadow_mdc.services import pan as pan_mod
    from shadow_mdc.services.pan import MAX_IN_FLIGHT

    started = 0
    in_flight = 0
    max_seen = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal started
        path = request.url.path
        if path.endswith("/open/user/info"):
            if started == 0:
                started = 1
                return httpx.Response(
                    429, headers={"Retry-After": "0"}, json={"message": "slow down"}
                )
            return httpx.Response(
                200, json={"state": True, "code": 0, "data": {"user_id": "1"}}
            )
        return httpx.Response(404, json={"message": path})

    client = Pan115Client(client_id="100197303")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _bind_token(client)
    original_gap = pan_mod.RATE_LIMIT_SECONDS
    pan_mod.RATE_LIMIT_SECONDS = 0.01
    try:
        info = await client.fetch_user_info()
        assert info.get("user_id") == "1"

        async def one() -> None:
            nonlocal in_flight, max_seen
            async with client._inflight:
                in_flight += 1
                max_seen = max(max_seen, in_flight)
                await asyncio.sleep(0.05)
                in_flight -= 1

        await asyncio.gather(*[one() for _ in range(6)])
        assert max_seen <= MAX_IN_FLIGHT
    finally:
        pan_mod.RATE_LIMIT_SECONDS = original_gap
        await client.aclose()


@pytest.mark.asyncio
async def test_offline_exists_reconcile_wrong_dir_and_stale_clear() -> None:
    from shadow_mdc.services.pan import PanOfflineConflictError, is_offline_exists_code

    assert is_offline_exists_code(10008) is True
    assert is_offline_exists_code("10008") is True
    assert is_offline_exists_code(1) is False

    hash_a = "AABBCCDD" + ("0" * 32)
    phase = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/open/offline/add_task_urls"):
            phase["n"] += 1
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": [{"state": False, "code": 10008, "message": "task exists"}],
                },
            )
        if path.endswith("/open/offline/get_task_list"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": {
                        "page": 1,
                        "page_count": 1,
                        "count": 1,
                        "tasks": [{
                            "info_hash": hash_a,
                            "status": 1,
                            "percentDone": 10,
                            "file_id": "",
                            "wp_path_id": "999",
                            "name": "x",
                        }],
                    },
                },
            )
        return httpx.Response(404, json={"message": path})

    client = Pan115Client(client_id="100197303")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _bind_token(client)
    with pytest.raises(PanOfflineConflictError):
        await client.submit_offline_url(
            f"magnet:?xt=urn:btih:{hash_a}",
            directory_id="42",
            info_hash_hint=hash_a,
        )
    await client.aclose()

    hash_b = "CCDDEEFF" + ("0" * 32)
    phase["n"] = 0

    def handler2(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/open/offline/add_task_urls"):
            phase["n"] += 1
            if phase["n"] == 1:
                return httpx.Response(
                    200,
                    json={
                        "state": True,
                        "code": 0,
                        "data": [{"state": False, "code": 10008, "message": "exists"}],
                    },
                )
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": [{"state": True, "code": 0, "info_hash": hash_b}],
                },
            )
        if path.endswith("/open/offline/get_task_list"):
            return httpx.Response(
                200,
                json={
                    "state": True,
                    "code": 0,
                    "data": {
                        "page": 1,
                        "page_count": 1,
                        "count": 1,
                        "tasks": [{
                            "info_hash": hash_b,
                            "status": 2,
                            "percentDone": 100,
                            "file_id": "gone",
                            "wp_path_id": "42",
                            "name": "old",
                        }],
                    },
                },
            )
        if path.endswith("/open/folder/get_info"):
            return httpx.Response(
                200, json={"state": False, "code": 430004, "message": "not found"}
            )
        if path.endswith("/open/offline/del_task"):
            return httpx.Response(200, json={"state": True, "code": 0})
        return httpx.Response(404, json={"message": path})

    client2 = Pan115Client(client_id="100197303")
    client2._http = httpx.AsyncClient(transport=httpx.MockTransport(handler2))
    _bind_token(client2)
    result = await client2.submit_offline_url(
        f"magnet:?xt=urn:btih:{hash_b}",
        directory_id="42",
        info_hash_hint=hash_b,
    )
    assert result["info_hash"] == hash_b
    assert result["adopted"] is False
    await client2.aclose()


@pytest.mark.asyncio
async def test_remove_offline_history_never_deletes_source() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/open/offline/del_task"):
            seen["body"] = request.content.decode("utf-8")
            return httpx.Response(200, json={"state": True, "code": 0})
        return httpx.Response(404)

    client = Pan115Client(client_id="100197303")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    _bind_token(client)
    await client.remove_offline_history("ABCD" + ("0" * 36))
    assert "del_source_file=0" in seen["body"]
    await client.aclose()
