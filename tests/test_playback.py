"""Playback resolve: Emby deep link + local sites file (no hardcoded remote hosts)."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.playback import (
    PlaybackSettings,
    PlaybackSitesFile,
    expand_site_url,
    extract_m3u8_urls,
    load_sites_from_settings,
    resolve_play,
    type_name_banned,
)


def test_expand_and_m3u8_parse() -> None:
    assert expand_site_url("https://example.invalid/v/{code_lower}", "ABC-123") == (
        "https://example.invalid/v/abc-123"
    )
    urls = extract_m3u8_urls(
        "线路1$host1$$$线路2$https://cdn.example.invalid/a.m3u8#备用$https://cdn.example.invalid/b.m3u8"
    )
    assert urls == [
        "https://cdn.example.invalid/a.m3u8",
        "https://cdn.example.invalid/b.m3u8",
    ]


def test_type_name_banned() -> None:
    assert type_name_banned("萝莉少女") is True
    assert type_name_banned("学生校园") is True
    assert type_name_banned("李宗瑞") is True
    assert type_name_banned("日本有码") is False
    assert type_name_banned("高清中字") is False


def test_load_sites_from_local_file(tmp_path: Path) -> None:
    path = tmp_path / "playback-sites.local.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "external_sites": [
                    {
                        "label": "Example",
                        "url_template": "https://example.invalid/{code_lower}",
                        "enabled": True,
                    }
                ],
                "vod_providers": [
                    {
                        "name": "ExCMS",
                        "base_url": "https://cms.example.invalid/api.php/provide/vod",
                        "whitelist_type_ids": [1, 2],
                        "enabled": True,
                    }
                ],
                "vod_max_providers": 2,
                "vod_max_sources": 4,
            }
        ),
        encoding="utf-8",
    )
    sites = load_sites_from_settings(
        PlaybackSettings(sites_path=str(path)), data_dir=tmp_path
    )
    assert len(sites.external_sites) == 1
    assert sites.external_sites[0].label == "Example"
    assert sites.vod_providers[0].whitelist_type_ids == [1, 2]


def test_load_sites_missing_file_is_empty(tmp_path: Path) -> None:
    sites = load_sites_from_settings(PlaybackSettings(), data_dir=tmp_path)
    assert sites.external_sites == []
    assert sites.vod_providers == []


@pytest.mark.asyncio
async def test_resolve_play_external_only() -> None:
    sites = PlaybackSitesFile.model_validate(
        {
            "external_sites": [
                {
                    "label": "Example",
                    "url_template": "https://example.invalid/{code_lower}",
                    "enabled": True,
                }
            ],
            "vod_providers": [],
            "vod_max_providers": 0,
            "vod_max_sources": 0,
        }
    )
    media = MediaServerSettings(enabled=False)
    async with httpx.AsyncClient() as client:
        result = await resolve_play(
            work_id="w1",
            code="SSIS-001",
            title=None,
            media=media,
            sites=sites,
            client=client,
        )
    assert result.preferred is not None
    assert result.preferred.kind == "external_site"
    assert result.preferred.url == "https://example.invalid/ssis-001"
    assert len(result.sources) == 1


@pytest.mark.asyncio
async def test_search_vod_skips_banned_and_non_whitelist() -> None:
    from shadow_mdc.services.playback import VodProviderConfig, search_vod_provider

    provider = VodProviderConfig(
        name="ExCMS",
        base_url="https://cms.example.invalid/api.php/provide/vod",
        whitelist_type_ids=[1, 99],
        enabled=True,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "list": [
                    {
                        "type_id": 99,
                        "type_name": "萝莉少女",
                        "vod_name": "SSIS-001 banned",
                        "vod_play_url": "https://cdn.example.invalid/bad.m3u8",
                    },
                    {
                        "type_id": 1,
                        "type_name": "日本有码",
                        "vod_name": "SSIS-001 ok",
                        "vod_play_url": "HD$https://cdn.example.invalid/ok.m3u8",
                    },
                    {
                        "type_id": 2,
                        "type_name": "日本有码",
                        "vod_name": "not in whitelist",
                        "vod_play_url": "https://cdn.example.invalid/skip.m3u8",
                    },
                ]
            },
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        sources = await search_vod_provider(provider, client, code="SSIS-001")
    assert len(sources) == 1
    assert sources[0].url.endswith("/ok.m3u8")
    assert sources[0].playable_in_app is True


def test_play_api_with_local_sites(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from datetime import date

    from shadow_mdc.domain import ProviderRecord
    from shadow_mdc.enums import ContentFamily, MediaCategory

    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "playback-sites.local.json").write_text(
        json.dumps(
            {
                "version": 1,
                "external_sites": [
                    {
                        "label": "Example",
                        "url_template": "https://example.invalid/{code_lower}",
                        "enabled": True,
                    }
                ],
                "vod_providers": [],
                "vod_max_providers": 0,
                "vod_max_sources": 0,
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")
    with TestClient(app) as client:
        missing = client.get("/api/works/no-such/play")
        assert missing.status_code == 404
        rt = client.app.state.runtime
        with rt.database.session() as session:
            from shadow_mdc.db.repository import Repository

            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="play-fixture",
                    title="Play fixture",
                    code="ABC-001",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    source_url="https://example.invalid/v/play-fixture",
                    release_date=date(2024, 1, 1),
                ),
                overwrite=False,
            )
            session.commit()
            work_id = work.id
        resolved = client.get(f"/api/works/{work_id}/play")
        assert resolved.status_code == 200
        body = resolved.json()
        assert body["sites_loaded"] is True
        assert body["external_site_count"] == 1
        assert any(s["url"] == "https://example.invalid/abc-001" for s in body["sources"])
        player = client.get(f"/api/works/{work_id}/player")
        assert player.status_code == 200
        assert "hls.js" in player.text
        assert "text/html" in player.headers.get("content-type", "")


# --- LumenBox-inspired hardening (rewritten): failover order + m3u8 gate ---


def test_validate_m3u8_playlist_text_gate() -> None:
    from shadow_mdc.services.playback import validate_m3u8_playlist_text

    assert validate_m3u8_playlist_text("") == "empty"
    assert validate_m3u8_playlist_text("<html>error</html>") == "html"
    assert validate_m3u8_playlist_text("not a playlist") == "not_m3u8"
    assert validate_m3u8_playlist_text("#EXTM3U\n#EXT-X-VERSION:3\n") == "no_segments"
    assert (
        validate_m3u8_playlist_text("#EXTM3U\n#EXTINF:4.0,\nseg0.ts\n#EXT-X-ENDLIST\n")
        is None
    )
    assert (
        validate_m3u8_playlist_text(
            "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=800000\nvariant.m3u8\n"
        )
        is None
    )
    # BOM + leading whitespace still ok
    assert (
        validate_m3u8_playlist_text("\ufeff  \n#EXTM3U\n#EXTINF:1,\na.ts\n") is None
    )


@pytest.mark.asyncio
async def test_probe_m3u8_rejects_html_empty_truncated_and_accepts_media() -> None:
    from shadow_mdc.services.playback import clear_m3u8_probe_cache, probe_m3u8_url

    clear_m3u8_probe_cache()
    media = "#EXTM3U\n#EXTINF:2.0,\nhttps://cdn.example.invalid/a.ts\n#EXT-X-ENDLIST\n"
    master = "#EXTM3U\n#EXT-X-STREAM-INF:BANDWIDTH=1\nhi.m3u8\n"
    huge = "#EXTM3U\n" + ("#EXTINF:1,\nx.ts\n" * 80_000)

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path.endswith("/ok.m3u8"):
            return httpx.Response(200, text=media)
        if path.endswith("/master.m3u8"):
            return httpx.Response(200, text=master)
        if path.endswith("/html.m3u8"):
            return httpx.Response(200, text="<html>Not Found</html>")
        if path.endswith("/empty.m3u8"):
            return httpx.Response(200, content=b"")
        if path.endswith("/big.m3u8"):
            return httpx.Response(200, text=huge)
        if path.endswith("/404.m3u8"):
            return httpx.Response(404, text="missing")
        return httpx.Response(500, text="err")

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        ok = await probe_m3u8_url(
            client, "https://cdn.example.invalid/ok.m3u8", use_cache=False
        )
        assert ok.ok and ok.reason == "ok"
        master_r = await probe_m3u8_url(
            client, "https://cdn.example.invalid/master.m3u8", use_cache=False
        )
        assert master_r.ok
        html = await probe_m3u8_url(
            client, "https://cdn.example.invalid/html.m3u8", use_cache=False
        )
        assert not html.ok and html.reason == "html"
        empty = await probe_m3u8_url(
            client, "https://cdn.example.invalid/empty.m3u8", use_cache=False
        )
        assert not empty.ok and empty.reason == "empty"
        big = await probe_m3u8_url(
            client,
            "https://cdn.example.invalid/big.m3u8",
            max_bytes=2048,
            use_cache=False,
        )
        assert not big.ok and big.reason == "truncated"
        missing = await probe_m3u8_url(
            client, "https://cdn.example.invalid/404.m3u8", use_cache=False
        )
        assert not missing.ok and missing.reason == "http_404"


@pytest.mark.asyncio
async def test_resolve_play_failover_order_emby_then_vod_probe(monkeypatch: pytest.MonkeyPatch) -> None:
    """Emby preferred when in library; bad m3u8 skipped; good m3u8 kept; failures surfaced."""

    from shadow_mdc.services import playback as playback_mod
    from shadow_mdc.services.playback import (
        PlaybackSitesFile,
        clear_m3u8_probe_cache,
        resolve_play,
    )

    clear_m3u8_probe_cache()
    call_order: list[str] = []

    async def fake_emby(settings, client, *, code, timeout=12.0):  # noqa: ANN001
        call_order.append("emby")
        return {
            "Id": "item-1",
            "Name": code,
            "ServerId": "srv",
            "Type": "Movie",
            "Path": f"/media/{code}.mp4",
        }

    monkeypatch.setattr(playback_mod, "resolve_emby_item", fake_emby)

    sites = PlaybackSitesFile.model_validate(
        {
            "external_sites": [
                {
                    "label": "Example",
                    "url_template": "https://example.invalid/{code_lower}",
                    "enabled": True,
                }
            ],
            "vod_providers": [
                {
                    "name": "BadCMS",
                    "base_url": "https://cms-bad.example.invalid/api.php/provide/vod",
                    "whitelist_type_ids": [1],
                    "enabled": True,
                    "timeout_seconds": 5,
                },
                {
                    "name": "GoodCMS",
                    "base_url": "https://cms-good.example.invalid/api.php/provide/vod",
                    "whitelist_type_ids": [1],
                    "enabled": True,
                    "timeout_seconds": 5,
                },
            ],
            "vod_max_providers": 4,
            "vod_max_sources": 4,
            "resolve_budget_ms": 12_000,
            "m3u8_probe_enabled": True,
            "m3u8_probe_timeout_seconds": 3.0,
            "m3u8_max_bytes": 65_536,
        }
    )
    media = MediaServerSettings(
        enabled=True,
        kind="emby",
        base_url="https://emby.example.invalid",
        api_key="test-key",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host or ""
        path = request.url.path
        if "cms-bad" in host:
            call_order.append("vod_search:BadCMS")
            return httpx.Response(
                200,
                json={
                    "list": [
                        {
                            "type_id": 1,
                            "type_name": "日本有码",
                            "vod_name": "SSIS-001 bad",
                            "vod_play_url": "HD$https://cdn.example.invalid/bad.m3u8",
                        }
                    ]
                },
            )
        if "cms-good" in host:
            call_order.append("vod_search:GoodCMS")
            return httpx.Response(
                200,
                json={
                    "list": [
                        {
                            "type_id": 1,
                            "type_name": "日本有码",
                            "vod_name": "SSIS-001 ok",
                            "vod_play_url": "HD$https://cdn.example.invalid/ok.m3u8",
                        }
                    ]
                },
            )
        if path.endswith("/bad.m3u8"):
            call_order.append("probe:bad")
            return httpx.Response(200, text="<html>gateway error</html>")
        if path.endswith("/ok.m3u8"):
            call_order.append("probe:ok")
            return httpx.Response(
                200,
                text="#EXTM3U\n#EXTINF:1.0,\nseg.ts\n#EXT-X-ENDLIST\n",
            )
        return httpx.Response(404)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await resolve_play(
            work_id="w1",
            code="SSIS-001",
            title=None,
            media=media,
            sites=sites,
            client=client,
            vod_client=client,
        )

    assert call_order[0] == "emby"
    assert "vod_search:BadCMS" in call_order
    assert "probe:bad" in call_order
    assert "vod_search:GoodCMS" in call_order
    assert "probe:ok" in call_order
    # Emby is preferred / chosen when in library
    assert result.emby_in_library is True
    assert result.preferred is not None
    assert result.preferred.kind == "emby"
    assert result.chosen is not None
    assert result.chosen.kind == "emby"
    # External always present; only probed-ok VOD kept
    assert any(s.kind == "external_site" for s in result.sources)
    vod = [s for s in result.sources if s.kind == "vod_m3u8"]
    assert len(vod) == 1
    assert vod[0].url.endswith("/ok.m3u8")
    assert vod[0].probe_ok is True
    assert any(f.stage == "m3u8_probe" and f.reason == "html" for f in result.failures)
    assert result.resolve_ms >= 0


@pytest.mark.asyncio
async def test_resolve_play_skips_failed_probe_without_emby() -> None:
    from shadow_mdc.services.playback import (
        PlaybackSitesFile,
        clear_m3u8_probe_cache,
        resolve_play,
    )

    clear_m3u8_probe_cache()
    sites = PlaybackSitesFile.model_validate(
        {
            "external_sites": [],
            "vod_providers": [
                {
                    "name": "OnlyBad",
                    "base_url": "https://cms.example.invalid/api.php/provide/vod",
                    "whitelist_type_ids": [1],
                    "enabled": True,
                }
            ],
            "vod_max_providers": 2,
            "vod_max_sources": 2,
            "m3u8_probe_enabled": True,
        }
    )
    media = MediaServerSettings(enabled=False)

    def handler(request: httpx.Request) -> httpx.Response:
        if "provide/vod" in str(request.url):
            return httpx.Response(
                200,
                json={
                    "list": [
                        {
                            "type_id": 1,
                            "type_name": "日本有码",
                            "vod_name": "SSIS-001",
                            "vod_play_url": "https://cdn.example.invalid/trunc.m3u8",
                        }
                    ]
                },
            )
        # Oversized body → truncated gate failure
        body = "#EXTM3U\n" + ("#EXTINF:1,\nx.ts\n" * 50_000)
        return httpx.Response(200, text=body)

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(transport=transport) as client:
        result = await resolve_play(
            work_id="w2",
            code="SSIS-001",
            title=None,
            media=media,
            sites=sites,
            client=client,
            vod_client=client,
        )
    assert result.sources == []
    assert result.preferred is None
    assert result.chosen is None
    assert any(f.reason == "truncated" for f in result.failures)
