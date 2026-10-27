from __future__ import annotations

import asyncio
import json
import subprocess
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from pathlib import Path

import httpx
import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import IdentityHints, ProviderRecord
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.media.nfo_import import (
    _to_provider_record,
    apply_nfo_locked_fields,
    parse_movie_nfo,
    resolve_sidecar_nfo,
    sibling_artwork_paths,
)
from shadow_mdc.media.probe import probe_media_info
from shadow_mdc.providers import base as providers_module
from shadow_mdc.providers.base import HttpProvider, ProviderError, ProviderRegistry
from shadow_mdc.services.scanner import Scanner
from shadow_mdc.services.task_events import TaskEventHub
from tests.test_provider_registry import FixtureProvider


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    with database.session() as session:
        yield Repository(session)


def _nfo(path: Path, body: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(f"<movie>{body}</movie>", encoding="utf-8")
    return path


def test_same_title_nfos_create_distinct_works(repository: Repository, tmp_path: Path) -> None:
    root = tmp_path / "library"
    for name in ("first", "second"):
        _nfo(root / name / "movie.nfo", "<title>Same title</title>")
        (root / name / "film.strm").write_text("https://example.com/video.mp4", encoding="utf-8")
    library = repository.create_library(
        name="Fixture",
        root_path=str(root),
        recursive=True,
        organize_template="{title}",
    )
    result = Scanner(repository).scan(library)
    assets = repository.list_assets()
    assert result.discovered == 2 and not result.errors
    assert len({asset.work_id for asset in assets}) == 2
    assert all(asset.work_id is not None for asset in assets)


def test_nfo_path_identity_survives_title_edits_and_exported_identity(tmp_path: Path) -> None:
    path = _nfo(tmp_path / "movie.nfo", "<title>Original</title>")
    first = _to_provider_record(parse_movie_nfo(path))
    _nfo(path, "<title>Edited</title>")
    assert _to_provider_record(parse_movie_nfo(path)).external_id == first.external_id
    _nfo(path, f'<title>Rebuilt</title><uniqueid type="nfo-import">{first.external_id}</uniqueid>')
    assert _to_provider_record(parse_movie_nfo(path)).external_id == first.external_id


@pytest.mark.parametrize(
    "identity",
    [
        "<id>tt1234567</id>",
        '<id type="imdb">tt1234567</id>',
        '<uniqueid type="IMDb">tt1234567</uniqueid>',
    ],
)
def test_nfo_external_ids_do_not_become_jav_codes(tmp_path: Path, identity: str) -> None:
    record = _to_provider_record(
        parse_movie_nfo(_nfo(tmp_path / "movie.nfo", f"<title>Film</title>{identity}"))
    )
    assert record.code is None
    assert record.family is ContentFamily.UNKNOWN
    assert record.external_id == "imdb:tt1234567"


def test_nfo_runtime_locks_and_nested_set(repository: Repository, tmp_path: Path) -> None:
    parsed = parse_movie_nfo(
        _nfo(
            tmp_path / "movie.nfo",
            "<title>Film</title><runtime>90.5</runtime><lockedfields>Runtime</lockedfields>"
            "<set><name>Series</name></set><tag>   </tag><tag>Tag</tag><genre>Tag</genre>"
            "<actor><name>Actor</name></actor><actor><name>Actor</name></actor>",
        )
    )
    record = _to_provider_record(parsed)
    work = repository.upsert_provider_record(
        record.model_copy(update={"runtime_seconds": 100}), overwrite=False
    )
    assert parsed.runtime_seconds == 5430 and parsed.series == "Series"
    assert parsed.tags == ("Tag",) and parsed.actors == ("Actor",)
    assert apply_nfo_locked_fields(repository, work, parsed) == ["runtime_seconds"]
    repository.upsert_provider_record(record.model_copy(update={"runtime_seconds": 200}), overwrite=True)
    assert work.runtime_seconds == 5430


def test_nfo_and_artwork_match_specific_media_before_directory_defaults(tmp_path: Path) -> None:
    media = tmp_path / "Film-CD1.mp4"
    _nfo(tmp_path / "movie.nfo", "<title>Shared</title>")
    exact = _nfo(tmp_path / "Film.nfo", "<title>Specific</title>")
    assert resolve_sidecar_nfo(media) == exact
    (tmp_path / "poster.jpg").write_bytes(b"generic")
    specific = tmp_path / "Film-CD1-poster.webp"
    specific.write_bytes(b"specific")
    assert sibling_artwork_paths(media)["poster"] == specific


def test_probe_timeout_does_not_stop_scan(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = tmp_path / "library"
    root.mkdir()
    for name in ("bad.mp4", "good.mp4"):
        (root / name).write_bytes(b"fixture")

    def probe(args: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if args[-1].endswith("bad.mp4"):
            raise subprocess.TimeoutExpired(args, 30)
        return subprocess.CompletedProcess(args, 0, '{"format":{"duration":"60"}}', "")

    monkeypatch.setattr("shadow_mdc.media.probe.shutil.which", lambda _: "ffprobe")
    monkeypatch.setattr("shadow_mdc.media.probe.subprocess.run", probe)
    library = repository.create_library(
        name="Fixture",
        root_path=str(root),
        recursive=True,
        organize_template="{title}",
    )
    result = Scanner(repository).scan(library)
    assert result.discovered == 2 and not result.errors
    durations = {Path(asset.path).name: asset.duration_seconds for asset in repository.list_assets()}
    assert durations == {"bad.mp4": None, "good.mp4": 60}


@pytest.mark.parametrize("payload", ["[]", "null", '{"format":{"duration":"NaN"}}'])
def test_probe_tolerates_malformed_shapes_and_nonfinite_numbers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    payload: str,
) -> None:
    monkeypatch.setattr("shadow_mdc.media.probe.shutil.which", lambda _: "ffprobe")
    monkeypatch.setattr(
        "shadow_mdc.media.probe.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess("ffprobe", 0, payload, ""),
    )
    assert probe_media_info(tmp_path / "film.mkv").duration_seconds is None


def test_probe_ignores_attached_cover_art(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    payload = {
        "streams": [
            {"codec_type": "video", "width": 800, "height": 800, "disposition": {"attached_pic": 1}},
            {"codec_type": "video", "width": 1920, "height": 1080, "duration": "90", "avg_frame_rate": "inf"},
        ]
    }
    monkeypatch.setattr("shadow_mdc.media.probe.shutil.which", lambda _: "ffprobe")
    monkeypatch.setattr(
        "shadow_mdc.media.probe.subprocess.run",
        lambda *args, **kwargs: subprocess.CompletedProcess("ffprobe", 0, json.dumps(payload), ""),
    )
    info = probe_media_info(tmp_path / "film.mkv")
    assert info.width == 1920 and info.duration_seconds == 90 and info.frame_rate is None


def test_scan_cancellation_stops_before_directory_enumeration(
    repository: Repository,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    library = repository.create_library(
        name="Fixture",
        root_path=str(tmp_path),
        recursive=True,
        organize_template="{title}",
    )

    def forbidden(*args: object) -> None:
        raise AssertionError("cancelled scan must not enumerate directories")

    monkeypatch.setattr("shadow_mdc.services.scanner.os.scandir", forbidden)
    assert Scanner(repository).scan(library, cancelled=lambda: True).discovered == 0


@pytest.mark.asyncio
async def test_task_events_deliver_initial_snapshot_and_thread_notifications() -> None:
    hub = TaskEventHub()
    event = await hub.subscribe()
    changes = hub.changes(event, heartbeat_seconds=1)
    assert await asyncio.wait_for(anext(changes), 0.1) == "tasks"
    await asyncio.to_thread(hub.notify)
    assert await asyncio.wait_for(anext(changes), 0.1) == "tasks"
    await changes.aclose()
    await hub.unsubscribe(event)


@pytest.mark.parametrize("status", [429, 503])
@pytest.mark.asyncio
async def test_retry_after_stops_immediate_retries_and_transport_fallback(
    monkeypatch: pytest.MonkeyPatch,
    status: int,
) -> None:
    attempts = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        return httpx.Response(status, headers={"Retry-After": "60"}, text="busy")

    async def forbidden(*args: object, **kwargs: object) -> None:
        raise AssertionError("rate limits must not trigger another transport")

    monkeypatch.setattr(providers_module, "impersonated_get", forbidden)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ProviderError) as error:
            await HttpProvider(client, retries=3)._get_text("fixture", "https://example.com")
    assert error.value.reason == "rate_limited" and error.value.retry_after_seconds == 60
    assert attempts == 1


def test_retry_after_accepts_http_dates_and_ignores_invalid_values() -> None:
    deadline = format_datetime(datetime.now(UTC) + timedelta(seconds=60), usegmt=True)
    delay = providers_module._retry_after_seconds(deadline)
    assert delay is not None and 58 < delay <= 60
    for invalid in ("NaN", "inf", "not a date"):
        assert providers_module._retry_after_seconds(invalid) is None


@pytest.mark.asyncio
async def test_provider_search_deadline_keeps_healthy_results_and_releases_slot() -> None:
    stopped = asyncio.Event()

    class HangingProvider(FixtureProvider):
        async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
            try:
                await asyncio.Event().wait()
                return []
            finally:
                stopped.set()

    record = ProviderRecord(provider="healthy", external_id="fixture", title="Fixture")
    registry = ProviderRegistry(
        [HangingProvider("slow"), FixtureProvider("healthy", result=record)],
        call_timeout_seconds=0.02,
        max_concurrent_calls=1,
    )
    hints = IdentityHints(term="ABC-123", mode=QueryMode.CODE, code="ABC-123", family=ContentFamily.JAV)
    result = await asyncio.wait_for(registry.search(hints), 1)
    assert result.records == (record,) and result.failures[0].reason == "timeout"
    assert stopped.is_set()


@pytest.mark.asyncio
async def test_queued_provider_calls_recheck_new_cooldown() -> None:
    calls = 0

    class RateLimitedProvider(FixtureProvider):
        async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
            nonlocal calls
            calls += 1
            await asyncio.sleep(0)
            raise ProviderError("limited", "rate_limited", "busy", retry_after_seconds=60)

    registry = ProviderRegistry([RateLimitedProvider("limited")], max_concurrent_calls=1)
    hints = IdentityHints(term="ABC-123", mode=QueryMode.CODE, code="ABC-123", family=ContentFamily.JAV)
    first, second = await asyncio.gather(registry.search(hints), registry.search(hints))
    assert first.failures[0].reason == "rate_limited" and second.failures[0].reason == "cooldown"
    assert calls == 1
