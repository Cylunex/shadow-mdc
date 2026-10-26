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
async def test_resolve_play_skips_banned_vod_type() -> None:
    sites = PlaybackSitesFile.model_validate(
        {
            "external_sites": [],
            "vod_providers": [
                {
                    "name": "ExCMS",
                    "base_url": "https://cms.example.invalid/api.php/provide/vod",
                    "whitelist_type_ids": [1, 99],
                    "enabled": True,
                }
            ],
            "vod_max_providers": 1,
            "vod_max_sources": 5,
        }
    )
    media = MediaServerSettings(enabled=False)

    def handler(request: httpx.Request) -> httpx.Response:
        assert "wd=SSIS-001" in str(request.url) or "wd=SSIS-001" in request.url.query.decode()
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
        result = await resolve_play(
            work_id="w1",
            code="SSIS-001",
            title=None,
            media=media,
            sites=sites,
            client=client,
        )
    assert len(result.sources) == 1
    assert result.sources[0].url.endswith("/ok.m3u8")
    assert result.sources[0].playable_in_app is True


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
