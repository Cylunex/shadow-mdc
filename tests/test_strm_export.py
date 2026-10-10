"""115 → STRM export, relay, rewrite, reconcile, Emby notify, pacing (no network)."""

from __future__ import annotations

import asyncio
import json
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.services import pan as pan_module
from shadow_mdc.services import strm_export
from shadow_mdc.services.emby_notify import EmbyNotifier, post_media_updated
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.pan import (
    Pan115Client,
    PanApiError,
    PanCredentials,
    PanSettings,
    RemoteVideo,
    ScanPacer,
    build_strm_locator,
    is_file_gone_error,
)
from shadow_mdc.services.strm_export import (
    build_relay_locator,
    export_work,
    map_to_emby_path,
    parse_relay_file_id,
    plan_strm_entries,
    read_sidecar,
    reconcile_deleted,
    rewrite_strm_tree,
)
from shadow_mdc.services.strm_relay import RelayError, StrmRelay, token_ok


def _relay_settings(root: Path, **extra: Any) -> PanSettings:
    values: dict[str, Any] = {
        "strm_enabled": True,
        "strm_output_root": str(root),
        "strm_mode": "relay",
        "strm_public_base_url": "http://nas.lan:8700",
        "strm_token": "t1",
    }
    values.update(extra)
    return PanSettings(**values)


def _video(fid: str, name: str, size: int | None = None, rel: str | None = None) -> RemoteVideo:
    return RemoteVideo(file_id=fid, name=name, pick_code=f"pc{fid}", relative_path=rel or name, size=size)


# ------------------------------------------------------------------ naming


def test_plan_single_and_multipart_names() -> None:
    single = plan_strm_entries("ABC-123", [_video("1", "abc-123.mp4")])
    assert [entry.name for entry in single] == ["ABC-123.strm"]

    multi = plan_strm_entries(
        "ABC-123",
        [
            _video("2", "ABC-123-CD2.mp4", 4_000),
            _video("1", "ABC-123-CD1.mp4", 4_000),
            _video("9", "sample.mp4", 10),  # tiny extra dropped
        ],
    )
    assert [(entry.name, entry.file_id) for entry in multi] == [
        ("ABC-123-cd1.strm", "1"),
        ("ABC-123-cd2.strm", "2"),
    ]


def test_locators_relay_and_openlist_compat() -> None:
    url = build_relay_locator("http://nas.lan:8700/", "12345", "s3cr=t")
    assert url == "http://nas.lan:8700/api/strm/play/12345?token=s3cr%3Dt"
    assert parse_relay_file_id(url) == "12345"
    assert parse_relay_file_id("http://openlist:5244/d/115/a.mp4") is None
    with pytest.raises(ValueError):
        build_relay_locator("", "1")
    with pytest.raises(ValueError):
        build_relay_locator("http://x", "../etc")
    # OpenList mode keeps the legacy body shape.
    assert strm_export.build_openlist_locator("http://openlist:5244/d/115/", "/云下载/ABC 1.mp4") == (
        build_strm_locator(prefix="http://openlist:5244/d/115/", relative_path="/云下载/ABC 1.mp4")
    )



def test_export_relative_dir_studio_code_hierarchy() -> None:
    from shadow_mdc.services.strm_export import export_relative_dir

    work = SimpleNamespace(studio="SODクリエイト", category="Japan", title="x", primary_code="STARS-145", tags=[])
    assert export_relative_dir("STARS-145", work) == Path("SODクリエイト") / "STARS-145"  # type: ignore[arg-type]
    assert export_relative_dir("STARS-145", None) == Path("Unknown Studio") / "STARS-145"
    assert export_relative_dir("STARS-145", work, template="{group}/{subgroup}/{studio}/{code}") == (  # type: ignore[arg-type]
        Path("JAV") / "有码" / "SODクリエイト" / "STARS-145"
    )


# ------------------------------------------------------------------ export order



def test_export_writes_artwork_then_nfo_then_strm_atomically(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    art = tmp_path / "art"
    art.mkdir()
    (art / "poster.jpg").write_bytes(b"P")
    (art / "fanart.jpg").write_bytes(b"F")
    work = SimpleNamespace(
        id="w1",
        title="Title",
        original_title=None,
        primary_code="ABC-123",
        plot=None,
        release_date=date(2026, 9, 1),
        runtime_seconds=None,
        artwork=[
            {"kind": "poster", "local_path": str(art / "poster.jpg")},
            {"kind": "fanart", "local_path": str(art / "fanart.jpg")},
        ],
    )
    order: list[str] = []
    real_copy, real_nfo, real_strm = strm_export._atomic_copy, strm_export.write_nfo, strm_export.write_strm
    monkeypatch.setattr(
        strm_export, "_atomic_copy", lambda s, d: (order.append(f"art:{d.name}"), real_copy(s, d))[1]
    )
    monkeypatch.setattr(strm_export, "write_nfo", lambda p, c: (order.append("nfo"), real_nfo(p, c))[1])
    monkeypatch.setattr(strm_export, "build_nfo", lambda w, ids: "<movie><title>x</title></movie>")
    monkeypatch.setattr(
        strm_export, "write_strm", lambda p, loc: (order.append(f"strm:{Path(p).name}"), real_strm(p, loc))[1]
    )

    root = tmp_path / "emby"
    result = export_work(
        settings=_relay_settings(root),
        code="ABC-123",
        videos=[_video("11", "a-cd1.mp4", 100), _video("12", "a-cd2.mp4", 100)],
        work=work,  # type: ignore[arg-type]
    )
    kinds = [item.split(":")[0] for item in order]
    assert kinds.index("nfo") > max(i for i, k in enumerate(kinds) if k == "art")
    assert min(i for i, k in enumerate(kinds) if k == "strm") > kinds.index("nfo")
    assert order[-2:] == ["strm:ABC-123-cd1.strm", "strm:ABC-123-cd2.strm"]
    directory = root / "Unknown Studio" / "ABC-123"
    assert result.directory == directory
    assert (
        directory / "ABC-123-cd1.strm"
    ).read_text().strip() == "http://nas.lan:8700/api/strm/play/11?token=t1"
    assert (directory / "poster.jpg").read_bytes() == b"P"
    assert not list(directory.glob("*.tmp")) and not list(directory.glob(".*.tmp"))
    assert [entry.file_id for entry in read_sidecar(directory)] == ["11", "12"]


def test_export_bad_config_writes_nothing(tmp_path: Path) -> None:
    root = tmp_path / "emby"
    with pytest.raises(ValueError):
        export_work(
            settings=_relay_settings(root, strm_public_base_url=None),
            code="ABC-1",
            videos=[_video("1", "a.mp4")],
        )
    assert not (root / "Unknown Studio" / "ABC-1").exists()


def test_openlist_mode_export_uses_prefix(tmp_path: Path) -> None:
    settings = PanSettings(
        strm_enabled=True, strm_output_root=str(tmp_path), strm_url_prefix="http://ol:5244/d/115"
    )
    export_work(settings=settings, code="XYZ-9", videos=[_video("5", "x.mp4", rel="云下载/XYZ-9/x.mp4")])
    assert (
        tmp_path / "Unknown Studio" / "XYZ-9" / "XYZ-9.strm"
    ).read_text().strip() == "http://ol:5244/d/115/云下载/XYZ-9/x.mp4"


# ------------------------------------------------------------------ rewrite


def test_rewrite_rotates_token_and_base_in_place(tmp_path: Path) -> None:
    root = tmp_path / "emby"
    export_work(settings=_relay_settings(root), code="AAA-1", videos=[_video("101", "a.mp4")])
    # A hand-made relay .strm without sidecar is still rewritable by file id.
    manual = root / "manual" / "M.strm"
    manual.parent.mkdir(parents=True)
    manual.write_text("http://old:1/api/strm/play/777?token=t1\n")
    other = root / "keep" / "K.strm"
    other.parent.mkdir(parents=True)
    other.write_text("/mnt/media/k.mp4\n")

    rotated = _relay_settings(root, strm_public_base_url="https://media.example", strm_token="t2")
    result = rewrite_strm_tree(root, rotated)
    assert len(result.rewritten) == 2
    assert (
        root / "Unknown Studio" / "AAA-1" / "AAA-1.strm"
    ).read_text().strip() == "https://media.example/api/strm/play/101?token=t2"
    assert manual.read_text().strip() == "https://media.example/api/strm/play/777?token=t2"
    assert other.read_text().strip() == "/mnt/media/k.mp4"
    assert rewrite_strm_tree(root, rotated).rewritten == []

    no_token = _relay_settings(root, strm_public_base_url="https://media.example", strm_token=None)
    rewrite_strm_tree(root, no_token)
    assert (root / "Unknown Studio" / "AAA-1" / "AAA-1.strm").read_text().strip() == "https://media.example/api/strm/play/101"


def test_rewrite_openlist_to_relay_uses_sidecar(tmp_path: Path) -> None:
    settings = PanSettings(
        strm_enabled=True, strm_output_root=str(tmp_path), strm_url_prefix="http://ol/d/115"
    )
    export_work(settings=settings, code="B-2", videos=[_video("55", "b.mp4", rel="dl/b.mp4")])
    result = rewrite_strm_tree(tmp_path, _relay_settings(tmp_path, strm_token=None))
    assert len(result.rewritten) == 1
    assert (tmp_path / "Unknown Studio" / "B-2" / "B-2.strm").read_text().strip() == "http://nas.lan:8700/api/strm/play/55"
    # and back to OpenList via the remembered remote path
    rewrite_strm_tree(tmp_path, settings)
    assert (tmp_path / "Unknown Studio" / "B-2" / "B-2.strm").read_text().strip() == "http://ol/d/115/dl/b.mp4"


# ------------------------------------------------------------------ reconcile


def test_reconcile_removes_only_definitively_gone(tmp_path: Path) -> None:
    root = tmp_path / "emby"
    settings = _relay_settings(root)
    export_work(settings=settings, code="GONE-1", videos=[_video("g1", "a.mp4")])
    export_work(settings=settings, code="LIVE-1", videos=[_video("l1", "a.mp4")])
    export_work(
        settings=settings, code="HALF-1", videos=[_video("h1", "a-cd1.mp4", 9), _video("g2", "a-cd2.mp4", 9)]
    )
    export_work(settings=settings, code="UNK-1", videos=[_video("u1", "a.mp4")])
    states = {"g1": False, "g2": False, "l1": True, "h1": True, "u1": None}

    async def exists(file_id: str) -> bool | None:
        return states[file_id]

    result = asyncio.run(reconcile_deleted(root, exists))
    assert [path.name for path in result.removed] == ["GONE-1"]
    assert not (root / "Unknown Studio" / "GONE-1").exists()
    assert (root / "Unknown Studio" / "LIVE-1").is_dir() and (root / "Unknown Studio" / "HALF-1").is_dir() and (root / "Unknown Studio" / "UNK-1").is_dir()
    assert result.unknown == 1 and result.kept == 2 and root.is_dir()


def test_file_gone_error_codes() -> None:
    assert is_file_gone_error(PanApiError("gone", code=430004))
    assert not is_file_gone_error(PanApiError("rate", code=40140125))
    assert not is_file_gone_error(RuntimeError("x"))


def test_map_to_emby_path(tmp_path: Path) -> None:
    settings = _relay_settings(tmp_path, strm_emby_root="/media/strm")
    assert map_to_emby_path(tmp_path / "Unknown Studio" / "ABC-1", settings) == "/media/strm/Unknown Studio/ABC-1"
    assert map_to_emby_path(tmp_path / "Unknown Studio" / "ABC-1", _relay_settings(tmp_path)) == str(tmp_path / "Unknown Studio" / "ABC-1")


# ------------------------------------------------------------------ Emby notify


def test_emby_notifier_batches_debounced_paths() -> None:
    calls: list[dict[str, Any]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(
            {
                "url": str(request.url),
                "token": request.headers.get("X-Emby-Token"),
                "body": json.loads(request.content),
            }
        )
        return httpx.Response(204)

    settings = MediaServerSettings(enabled=True, kind="emby", base_url="http://emby:8096", api_key="k")

    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            notifier = EmbyNotifier(load_settings=lambda: settings, client=client, debounce_seconds=0.05)
            notifier.start()
            notifier.enqueue(["/m/A"], "Created")
            notifier.enqueue(["/m/B", "/m/A"], "Modified")  # Created wins for A
            notifier.enqueue(["/m/C"], "Deleted")
            await asyncio.sleep(0.3)
            await notifier.stop()

    asyncio.run(scenario())
    assert len(calls) == 1
    assert calls[0]["url"] == "http://emby:8096/Library/Media/Updated"
    assert calls[0]["token"] == "k"
    assert calls[0]["body"] == {
        "Updates": [
            {"Path": "/m/A", "UpdateType": "Created"},
            {"Path": "/m/B", "UpdateType": "Modified"},
            {"Path": "/m/C", "UpdateType": "Deleted"},
        ]
    }


def test_emby_notify_skips_without_settings() -> None:
    async def scenario() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r: httpx.Response(500))) as client:
            result = await post_media_updated(MediaServerSettings(), client, [("/a", "Created")])
            assert result.attempted is False
            result = await post_media_updated(
                MediaServerSettings(enabled=True, base_url="http://e"), client, [("/a", "Created")]
            )
            assert result.ok is False and result.attempted is False

    asyncio.run(scenario())


# ------------------------------------------------------------------ pacing / retries


def test_scan_pacer_base_plus_jitter() -> None:
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    pacer = ScanPacer(sleep=fake_sleep)

    async def run() -> None:
        for _ in range(5):
            await pacer.wait()

    asyncio.run(run())
    assert len(slept) == 4  # no wait before the first request
    assert all(0.35 <= value <= 0.5 for value in slept)


def _client_with(handler: Any) -> Pan115Client:
    client = Pan115Client(client_id="x")
    client._http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    client.bind_tokens(
        PanCredentials(access_token="a", refresh_token="r", expires_at="2999-01-01T00:00:00+00:00")
    )
    return client


@pytest.mark.parametrize(
    ("method", "status", "expected_calls"),
    [("POST", 502, 1), ("POST", 503, 1), ("GET", 502, 4), ("POST", 429, 4)],
)
def test_non_idempotent_requests_are_not_blindly_retried(
    monkeypatch: pytest.MonkeyPatch, method: str, status: int, expected_calls: int
) -> None:
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.method)
        return httpx.Response(status)

    async def no_sleep(_seconds: float) -> None:
        return None

    monkeypatch.setattr(pan_module.asyncio, "sleep", no_sleep)
    monkeypatch.setattr(pan_module, "RATE_LIMIT_SECONDS", 0.0)
    client = _client_with(handler)
    response = asyncio.run(client._request(method, "https://proapi.115.com/x", bearer=True))
    assert response.status_code == status
    assert len(calls) == expected_calls


def test_walk_videos_paced_and_recursive(monkeypatch: pytest.MonkeyPatch) -> None:
    client = Pan115Client(client_id="x")
    tree = {
        "root": [
            {"id": "f1", "name": "ABC-1-cd1.mp4", "is_directory": False, "size": 10, "pick_code": "p1"},
            {"id": "d1", "name": "sub", "is_directory": True},
            {"id": "t1", "name": "readme.txt", "is_directory": False},
        ],
        "d1": [{"id": "f2", "name": "ABC-1-cd2.mkv", "is_directory": False, "size": 10, "pick_code": "p2"}],
    }

    async def folder_info(file_id: str) -> dict[str, Any]:
        return {"file_name": "ABC-1", "file_category": "0"}

    async def list_directory(directory_id: str, *, page: int = 1, limit: int = 100) -> dict[str, Any]:
        items = tree.get(directory_id, [])
        return {"items": items, "total": len(items)}

    monkeypatch.setattr(client, "get_folder_info", folder_info)
    monkeypatch.setattr(client, "list_directory", list_directory)
    slept: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        slept.append(seconds)

    videos = asyncio.run(client.walk_videos("root", pacer=ScanPacer(sleep=fake_sleep)))
    assert [(v.file_id, v.relative_path) for v in videos] == [
        ("f1", "ABC-1/ABC-1-cd1.mp4"),
        ("f2", "ABC-1/sub/ABC-1-cd2.mkv"),
    ]
    assert len(slept) == 2  # info + 2 list calls → 2 paced gaps


# ------------------------------------------------------------------ relay


class _FakeRelayClient:
    def __init__(self, *, download: str | Exception | None, play: str | None) -> None:
        self.download = download
        self.play = play
        self.uas: list[str] = []
        self.info_calls = 0

    async def get_folder_info(self, file_id: str) -> dict[str, Any]:
        self.info_calls += 1
        return {"pick_code": f"pick-{file_id}"} if file_id != "404" else {}

    async def download_url(self, pick_code: str, *, user_agent: str) -> str | None:
        self.uas.append(user_agent)
        if isinstance(self.download, Exception):
            raise self.download
        return self.download

    async def video_play_url(self, pick_code: str, *, user_agent: str) -> str | None:
        self.uas.append(user_agent)
        return self.play


def _fake_pan(client: _FakeRelayClient, connected: bool = True, ua: str | None = None) -> Any:
    return SimpleNamespace(
        status=lambda: {"connected": connected},
        get_client=lambda: client,
        config_store=SimpleNamespace(load=lambda: PanSettings(strm_user_agent=ua)),
    )


def test_relay_prefers_direct_download_and_caches() -> None:
    fake = _FakeRelayClient(download="https://cdn.115/direct", play="https://cdn.115/play.m3u8")
    relay = StrmRelay(_fake_pan(fake))
    first = asyncio.run(relay.resolve("42", "Emby/4.8"))
    second = asyncio.run(relay.resolve("42", "Emby/4.8"))
    assert first.url == "https://cdn.115/direct" and first.source == "download"
    assert second == first
    assert fake.uas == ["Emby/4.8"]  # cached, same UA used for 115 call
    assert fake.info_calls == 1


def test_relay_falls_back_to_play_url_and_default_ua() -> None:
    fake = _FakeRelayClient(download=PanApiError("no", code=1), play="https://cdn.115/play.m3u8")
    relay = StrmRelay(_fake_pan(fake, ua="MyUA/1"))
    target = asyncio.run(relay.resolve("7", None))
    assert target.source == "play"
    assert fake.uas == ["MyUA/1", "MyUA/1"]


def test_relay_errors() -> None:
    with pytest.raises(RelayError) as disconnected:
        asyncio.run(
            StrmRelay(_fake_pan(_FakeRelayClient(download=None, play=None), connected=False)).resolve(
                "1", "x"
            )
        )
    assert disconnected.value.status_code == 503
    with pytest.raises(RelayError) as missing:
        asyncio.run(StrmRelay(_fake_pan(_FakeRelayClient(download=None, play=None))).resolve("404", "x"))
    assert missing.value.status_code == 404
    with pytest.raises(RelayError) as nothing:
        asyncio.run(StrmRelay(_fake_pan(_FakeRelayClient(download=None, play=None))).resolve("5", "x"))
    assert nothing.value.status_code == 502
    assert token_ok(None, None) and token_ok("a", "a") and not token_ok("a", "b") and not token_ok("a", None)


def test_strm_play_endpoint_redirects_with_token(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'app.db'}")
    with TestClient(app) as client:
        runtime = app.state.runtime
        fake = _FakeRelayClient(download="https://cdn.115/direct?x=1", play=None)
        monkeypatch.setattr(runtime.strm_relay, "_pan", _fake_pan(fake))
        saved = client.put("/api/pan/settings", json={"strm_token": "sekret", "strm_mode": "relay"})
        assert saved.status_code == 200
        body = saved.json()
        assert body["strm_token_set"] is True and "strm_token" not in body
        assert body["strm_mode"] == "relay"

        assert client.get("/api/strm/play/123", follow_redirects=False).status_code == 403
        assert client.get("/api/strm/play/123?token=nope", follow_redirects=False).status_code == 403
        ok = client.get(
            "/api/strm/play/123?token=sekret", headers={"User-Agent": "Infuse/7"}, follow_redirects=False
        )
        assert ok.status_code == 302
        assert ok.headers["location"] == "https://cdn.115/direct?x=1"
        assert fake.uas == ["Infuse/7"]
        head = client.head(
            "/api/strm/play/123?token=sekret", headers={"User-Agent": "Infuse/7"}, follow_redirects=False
        )
        assert head.status_code == 302
        assert client.get("/api/strm/play/..%2Fx?token=sekret", follow_redirects=False).status_code in {
            400,
            404,
        }
        bad = client.put("/api/pan/settings", json={"strm_mode": "bogus"})
        assert bad.status_code == 422
        status = client.get("/api/pan/strm/status")
        assert status.status_code == 200


def test_download_and_play_url_parsing_send_player_ua() -> None:
    seen: list[tuple[str, str, str | None]] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append((request.method, request.url.path, request.headers.get("user-agent")))
        if request.url.path == "/open/ufile/downurl":
            assert b"pick_code=pc1" in request.content
            return httpx.Response(
                200, json={"state": True, "code": 0, "data": {"42": {"url": {"url": "https://cdn/x?s=1"}}}}
            )
        return httpx.Response(
            200,
            json={
                "state": True,
                "code": 0,
                "data": {
                    "video_url": [
                        {"url": "https://cdn/480.m3u8", "definition": 1},
                        {"url": "https://cdn/1080.m3u8", "definition": 4},
                    ]
                },
            },
        )

    client = _client_with(handler)
    assert asyncio.run(client.download_url("pc1", user_agent="Emby/4.9")) == "https://cdn/x?s=1"
    assert asyncio.run(client.video_play_url("pc1", user_agent="Emby/4.9")) == "https://cdn/1080.m3u8"
    assert seen == [("POST", "/open/ufile/downurl", "Emby/4.9"), ("GET", "/open/video/play", "Emby/4.9")]


def test_rematerialize_layout_moves_flat_to_studio_code(tmp_path: Path) -> None:
    from shadow_mdc.db.models import Work
    from shadow_mdc.services.strm_export import rematerialize_layout, write_sidecar, StrmEntry

    root = tmp_path / "emby"
    old = root / "AAA-1"
    old.mkdir(parents=True)
    (old / "AAA-1.strm").write_text("http://x/d/a\n", encoding="utf-8")
    write_sidecar(
        old,
        "AAA-1",
        [StrmEntry("AAA-1.strm", "/media/115/a.mp4", remote_path="/media/115/a.mp4")],
        work_id="w1",
    )
    work = Work(
        id="w1",
        title="t",
        primary_code="AAA-1",
        studio="Studio X",
        category="Japan",
        actors=[],
        tags=[],
        artwork=[],
        directors=[],
    )
    settings = PanSettings(
        strm_enabled=True,
        strm_output_root=str(root),
        strm_mode="openlist",
        openlist_base_url="http://ol",
        strm_layout_template="{studio}/{code}",
    )
    result = rematerialize_layout(
        root,
        settings,
        resolve_work=lambda code, work_id: work,
        identities_for=lambda item: [],
    )
    assert len(result.moved) == 1
    assert (root / "Studio X" / "AAA-1" / "AAA-1.strm").is_file()
    assert (root / "Studio X" / "AAA-1" / "movie.nfo").is_file()
    assert not old.exists()
    again = rematerialize_layout(
        root,
        settings,
        resolve_work=lambda code, work_id: work,
        identities_for=lambda item: [],
    )
    assert again.moved == []
    assert again.skipped == 1
