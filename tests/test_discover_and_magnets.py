from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from shadow_mdc.api import app
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.media.artwork import ensure_list_thumbnail
from shadow_mdc.media.magnets import parse_magnet_links_from_html
from shadow_mdc.media.nfo_import import resolve_sidecar_nfo
from shadow_mdc.services.discover import parse_javdb_list
from shadow_mdc.services.pan import pan_status


def test_parse_javdb_list_cards() -> None:
    html = """
    <div class="movie-list">
      <div class="item">
        <a href="/v/abc123">
          <img src="/covers/a.jpg" />
          <div class="video-title"><strong>SSIS-123</strong> Fixture title</div>
          <div class="meta">2025-02-03</div>
        </a>
      </div>
    </div>
    """
    items = parse_javdb_list(html, "https://javdb.com")
    assert len(items) == 1
    assert items[0].external_id == "abc123"
    assert items[0].code == "SSIS-123"
    assert items[0].thumb_url and items[0].thumb_url.endswith("/covers/a.jpg")


def test_parse_magnet_links_from_html() -> None:
    html = """
    <a href="magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=SSIS-123-C">m1</a>
    <a href="magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=dup">dup</a>
    """
    magnets = parse_magnet_links_from_html(html, provider="javdb")
    assert len(magnets) == 1
    assert magnets[0].info_hash == "ABCDEF0123456789ABCDEF0123456789ABCDEF01"
    assert magnets[0].has_subtitle is True


def test_resolve_sidecar_nfo_prefers_movie_nfo(tmp_path: Path) -> None:
    media = tmp_path / "SSIS-123.mp4"
    media.write_bytes(b"x")
    (tmp_path / "movie.nfo").write_text(
        "<movie><title>T</title><num>SSIS-123</num></movie>", encoding="utf-8"
    )
    (tmp_path / "SSIS-123.nfo").write_text("<movie><title>Other</title></movie>", encoding="utf-8")
    assert resolve_sidecar_nfo(media) == tmp_path / "movie.nfo"


def test_ensure_list_thumbnail(tmp_path: Path) -> None:
    source = tmp_path / "poster.jpg"
    Image.new("RGB", (1200, 800), color=(20, 40, 60)).save(source, format="JPEG")
    thumb = ensure_list_thumbnail(tmp_path, source, max_width=480)
    assert thumb is not None and thumb.is_file()
    with Image.open(thumb) as image:
        assert image.size[0] <= 480


def test_pan_status_stub() -> None:
    status = pan_status()
    assert status["available"] is False
    assert status["configured"] is False


def test_work_magnet_persistence_and_api(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'magnets.db'}")
    monkeypatch.setenv("SHADOW_MDC_TRANSLATION_ENABLED", "false")

    with TestClient(app) as client:
        pan = client.get("/api/pan/status")
        assert pan.status_code == 200
        assert pan.json()["available"] is False

        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="javdb-42",
                    title="SSIS-123 Fixture title",
                    code="SSIS-123",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    source_url="https://javdb.com/v/javdb-42",
                ),
                overwrite=True,
            )
            created, skipped = repo.save_work_magnets(
                work,
                [
                    {
                        "uri": "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=SSIS-123",
                        "info_hash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
                        "name": "SSIS-123",
                        "has_subtitle": False,
                        "hd": True,
                    }
                ],
                provider="javdb",
            )
            assert created == 1 and skipped == 0
            created2, skipped2 = repo.save_work_magnets(
                work,
                [
                    {
                        "uri": "magnet:?xt=urn:btih:ABCDEF0123456789ABCDEF0123456789ABCDEF01&dn=SSIS-123",
                        "info_hash": "ABCDEF0123456789ABCDEF0123456789ABCDEF01",
                    }
                ],
                provider="javdb",
            )
            assert created2 == 0 and skipped2 == 1
            work_id = work.id

        listed = client.get(f"/api/works/{work_id}/magnets")
        assert listed.status_code == 200
        assert len(listed.json()) == 1
        saved = client.post(
            f"/api/works/{work_id}/magnets",
            json={
                "provider": "javdb",
                "magnets": [
                    {
                        "provider": "javdb",
                        "info_hash": "1234567890ABCDEF1234567890ABCDEF12345678",
                        "uri": "magnet:?xt=urn:btih:1234567890ABCDEF1234567890ABCDEF12345678&dn=other",
                        "name": "other",
                        "has_subtitle": True,
                        "hd": False,
                    }
                ],
            },
        )
        assert saved.status_code == 200
        assert len(saved.json()) == 2
        detail = client.get(f"/api/works/{work_id}")
        assert detail.status_code == 200
        assert len(detail.json().get("magnets", [])) == 2
