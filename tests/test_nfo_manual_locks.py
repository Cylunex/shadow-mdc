"""NFO manual markers become per-field locks that survive multi-source re-scrapes."""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from xml.etree import ElementTree

import pytest

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily
from shadow_mdc.media.nfo import build_nfo
from shadow_mdc.media.nfo_import import apply_nfo_locked_fields, parse_movie_nfo


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    database = Database(f"sqlite:///{tmp_path / 'locks.db'}")
    database.initialize()
    with database.session() as session:
        yield Repository(session)


def _nfo(path: Path, extra: str) -> Path:
    path.write_text(
        "<?xml version='1.0' encoding='utf-8'?><movie>"
        "<title>ABC-123 我改的标题</title><id>ABC-123</id><plot>手写简介</plot>"
        "<studio>NFO Studio</studio><actor><name>演员甲</name></actor>"
        f"<premiered>2020-01-02</premiered>{extra}</movie>",
        encoding="utf-8",
    )
    return path


def _scraped(repository: Repository) -> object:
    return repository.upsert_provider_record(
        ProviderRecord(
            provider="javdb",
            external_id="abc123",
            code="ABC-123",
            title="Scraped title",
            family=ContentFamily.JAV,
            studio="Scraped Studio",
            plot="scraped plot",
            actors=("Scraped Actor",),
        ),
        overwrite=False,
    )


def test_parse_lock_markers(tmp_path: Path) -> None:
    own = parse_movie_nfo(_nfo(tmp_path / "a.nfo", "<shadowmdc_locks>title,plot,bogus</shadowmdc_locks>"))
    assert own.locked_fields == ("title", "plot")
    emby = parse_movie_nfo(_nfo(tmp_path / "b.nfo", "<lockedfields>Cast|Studios|Runtime</lockedfields>"))
    assert set(emby.locked_fields) == {"actors", "studio"}  # runtime absent in NFO → not locked
    whole = parse_movie_nfo(_nfo(tmp_path / "c.nfo", "<lockdata>true</lockdata>"))
    assert {"title", "plot", "studio", "actors", "release_date"} <= set(whole.locked_fields)
    assert "series" not in whole.locked_fields
    assert parse_movie_nfo(_nfo(tmp_path / "d.nfo", "")).locked_fields == ()


def test_nfo_locked_fields_survive_overwrite_scrape(repository: Repository, tmp_path: Path) -> None:
    work = _scraped(repository)
    parsed = parse_movie_nfo(_nfo(tmp_path / "movie.nfo", "<lockedfields>Name|Overview|Cast</lockedfields>"))
    applied = apply_nfo_locked_fields(repository, work, parsed)
    assert set(applied) == {"title", "plot", "actors"}
    assert work.title == "ABC-123 我改的标题"
    assert work.actors == ["演员甲"]
    assert {"title", "plot", "actors"} <= set(work.field_locks)
    assert work.field_sources["plot"] == "nfo-manual"

    # Forced re-scrape: unlocked studio refreshes, locked fields stay.
    repository.upsert_provider_record(
        ProviderRecord(
            provider="r18dev",
            external_id="abc123",
            code="ABC-123",
            title="Another scrape",
            family=ContentFamily.JAV,
            studio="Fresh Studio",
            plot="other plot",
            actors=("Other",),
        ),
        overwrite=True,
    )
    assert work.title == "ABC-123 我改的标题"
    assert work.plot == "手写简介"
    assert work.actors == ["演员甲"]


def test_in_app_lock_beats_nfo(repository: Repository, tmp_path: Path) -> None:
    work = _scraped(repository)
    repository.update_work_fields(work, title="App title")
    parsed = parse_movie_nfo(_nfo(tmp_path / "movie.nfo", "<shadowmdc_locks>title</shadowmdc_locks>"))
    assert apply_nfo_locked_fields(repository, work, parsed) == []
    assert work.title == "App title"


def test_nfo_never_wipes_cast(repository: Repository, tmp_path: Path) -> None:
    work = _scraped(repository)
    path = tmp_path / "movie.nfo"
    path.write_text(
        "<movie><title>T</title><id>ABC-123</id><shadowmdc_locks>actors,title</shadowmdc_locks></movie>",
        encoding="utf-8",
    )
    parsed = parse_movie_nfo(path)
    assert "actors" not in parsed.locked_fields
    apply_nfo_locked_fields(repository, work, parsed)
    assert work.actors == ["Scraped Actor"]


def test_build_nfo_round_trips_locks(repository: Repository, tmp_path: Path) -> None:
    work = _scraped(repository)
    repository.update_work_fields(work, plot="mine", actors=["A"])
    root = ElementTree.fromstring(build_nfo(work, []).split("\n", 1)[1])
    assert root.findtext("shadowmdc_locks") == "actors,plot"
    assert root.findtext("lockedfields") == "Cast|Overview"
    path = tmp_path / "movie.nfo"
    path.write_text(build_nfo(work, []), encoding="utf-8")
    assert set(parse_movie_nfo(path).locked_fields) == {"actors", "plot"}
