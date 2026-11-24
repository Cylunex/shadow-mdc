import asyncio
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image
from sqlalchemy import select

from shadow_mdc.api import app
from shadow_mdc.db.models import (
    Actor,
    ExternalIdentity,
    MediaAsset,
    SourceSnapshot,
    Work,
    WorkActor,
    WorkMagnet,
)
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import MediaTechnicalInfo, ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.identity import build_identity_hints
from shadow_mdc.services.catalog_people import person as lookup_catalog_person
from shadow_mdc.services.library_search import rank_work_dicts, score_work_fields
from shadow_mdc.services.subtitlecat import SubtitleCatFetcher
from shadow_mdc.services.translation import GoogleTitleTranslator, TranslationCache


def _jpeg_bytes(color: tuple[int, int, int]) -> bytes:
    buf = BytesIO()
    Image.new("RGB", (60, 90), color=color).save(buf, format="JPEG")
    return buf.getvalue()


@pytest.mark.parametrize("source_kind", ["poster", "sample"])
def test_prefer_work_poster_regenerates_thumb_and_sets_no_cache_headers(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    source_kind: str,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'poster.db'}")

    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            work = repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="sone201",
                    code="SONE-201",
                    title="Poster Refresh Fixture",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                ),
                overwrite=True,
            )
            work_id = work.id
            art_dir = data_dir / "artwork" / work_id
            art_dir.mkdir(parents=True, exist_ok=True)
            old_poster = art_dir / "poster.jpg"
            alt_poster = (art_dir / "samples" if source_kind == "sample" else art_dir) / "alt.jpg"
            alt_poster.parent.mkdir(exist_ok=True)
            old_thumb = art_dir / "thumb.jpg"
            old_poster.write_bytes(_jpeg_bytes((220, 20, 20)))
            old_thumb.write_bytes(_jpeg_bytes((220, 20, 20)))
            alt_poster.write_bytes(_jpeg_bytes((20, 40, 220)))
            work.artwork = [
                {"kind": "poster", "local_path": str(old_poster), "url": "https://img.example/1.jpg"},
                {"kind": source_kind, "local_path": str(alt_poster), "url": "https://img.example/2.jpg"},
            ]
            session.flush()

        old_thumb_bytes = (data_dir / "artwork" / work_id / "thumb.jpg").read_bytes()
        first_art = client.get(f"/api/works/{work_id}/artwork/poster")
        assert first_art.status_code == 200
        assert first_art.headers["cache-control"] == "private, no-cache"

        prefer = client.post(f"/api/works/{work_id}/artwork/prefer", json={"artwork_index": 1})
        assert prefer.status_code == 200
        new_thumb = data_dir / "artwork" / work_id / "thumb.jpg"
        assert new_thumb.is_file()
        assert new_thumb.read_bytes() != old_thumb_bytes
        assert client.get(f"/api/works/{work_id}/artwork/poster").content == alt_poster.read_bytes()


def test_work_edits_and_field_locks_are_saved_together(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'edit.db'}")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add(Work(id="edited", title="Before", actors=["Performer"], field_locks=["actors"]))
        path = "/api/works/edited"
        invalid = client.patch(path, json={"title": "Attempt", "actors": [], "field_locks": []})
        assert invalid.status_code == 422
        detail = client.get(path).json()
        assert (detail["title"], detail["field_locks"]) == ("Before", ["actors"])
        valid = client.patch(path, json={"title": "After", "field_locks": ["plot"]})
        assert valid.status_code == 200
        detail = client.get(path).json()
        assert (detail["title"], detail["field_locks"]) == ("After", ["plot"])


def test_merge_actors_persists_aliases_and_prevents_startup_resurrection(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'merge.db'}")
    database.initialize()

    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="sone301",
                code="SONE-301",
                title="Actor Merge Fixture",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                actors=("河北彩花",),
            ),
            overwrite=True,
        )
        repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="sone302",
                code="SONE-302",
                title="Actor Keep Fixture",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                actors=("河北彩伽",),
            ),
            overwrite=True,
        )
        actors = {actor.name: actor for actor in session.scalars(select(Actor))}
        keep = actors["河北彩伽"]
        drop = actors["河北彩花"]

        merged = repo.merge_actors(keep_actor_id=keep.id, drop_actor_id=drop.id)
        assert "河北彩花" in merged.aliases
        refreshed_work = repo.get_work(work.id)
        assert refreshed_work is not None
        assert refreshed_work.actors == ["河北彩伽"]

        repo.sync_all_work_actors()
        remaining_names = {actor.name for actor in session.scalars(select(Actor))}
        assert remaining_names == {"河北彩伽"}


def test_delete_work_cleans_child_rows_and_unlinks_assets(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'delete.db'}")
    database.initialize()
    media_file = tmp_path / "SONE-401.mp4"
    media_file.write_bytes(b"fixture")

    with database.session() as session:
        repo = Repository(session)
        library = repo.create_library(
            name="Main",
            root_path=str(tmp_path),
            recursive=True,
            organize_template="{code}",
        )
        stat = media_file.stat()
        asset, _created = repo.upsert_asset(
            library_id=library.id,
            path=str(media_file),
            size=stat.st_size,
            modified_ns=stat.st_mtime_ns,
            duration_seconds=None,
            oshash=None,
            hints=build_identity_hints("SONE-401.mp4"),
            media_info=MediaTechnicalInfo(),
        )
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="sone401",
                code="SONE-401",
                title="Delete Fixture",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                actors=("演员甲",),
            ),
            overwrite=True,
        )
        repo.attach_asset_to_work(asset, work)
        magnet_created, _ = repo.save_work_magnets(
            work,
            [{"uri": "magnet:?xt=urn:btih:" + "a" * 40, "info_hash": "a" * 40}],
            provider="sukebei",
        )
        assert magnet_created == 1
        work_id = work.id
        asset_id = asset.id

    with database.session() as session:
        repo = Repository(session)
        assert repo.delete_work(work_id) is True

    with database.session() as session:
        reloaded_asset = session.get(MediaAsset, asset_id)
        assert reloaded_asset is not None
        assert reloaded_asset.work_id is None
        identities = session.scalars(
            select(ExternalIdentity).where(ExternalIdentity.work_id == work_id)
        ).all()
        assert identities == []
        assert session.scalars(select(WorkActor).where(WorkActor.work_id == work_id)).all() == []
        assert session.scalars(select(WorkMagnet).where(WorkMagnet.work_id == work_id)).all() == []
        assert session.scalars(select(SourceSnapshot).where(SourceSnapshot.work_id == work_id)).all() == []


def test_bulk_translate_advances_past_completed_and_locked_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'translate_bulk.db'}")

    def handle(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=[[["新翻译标题"]]])

    transport = httpx.MockTransport(handle)
    with TestClient(app) as client, httpx.Client() as _:
        async_client = httpx.AsyncClient(transport=transport)
        try:
            cache = TranslationCache(data_dir / "tr_cache.db")
            translator = GoogleTitleTranslator(
                async_client,
                cache,
                enabled=True,
                endpoint="https://translate.example/translate",
                target_language="zh-CN",
            )
            app.state.runtime = replace(app.state.runtime, translator=translator)
            with app.state.runtime.database.session() as session:
                repo = Repository(session)
                done_work = repo.upsert_provider_record(
                    ProviderRecord(
                        provider="javdb",
                        external_id="done1",
                        code="SONE-501",
                        title="已完成中文标题",
                        family=ContentFamily.JAV,
                    ),
                    overwrite=True,
                )
                done_work.field_sources = {
                    **dict(done_work.field_sources or {}),
                    "title": "translation:google",
                }
                pending_work = repo.upsert_provider_record(
                    ProviderRecord(
                        provider="javdb",
                        external_id="todo2",
                        code="SONE-502",
                        title="まだ翻訳されていないタイトル",
                        family=ContentFamily.JAV,
                    ),
                    overwrite=True,
                )
                pending_id = pending_work.id
                locked_work = Work(
                    id="locked", title="タイトル", family=ContentFamily.JAV.value, field_locks=["title"],
                    created_at=datetime.now(UTC) + timedelta(seconds=1),
                )
                session.add(locked_work)
                cache.put("タイトル", "zh-CN", "title", "缓存译文")

            res = client.post("/api/works/translate", json={"limit": 1})
            assert res.status_code == 200
            body = res.json()
            assert body["translated"] == 1
            assert body["remaining"] == 0
            assert client.get("/api/works/locked").json()["title"] == "タイトル"
            detail = client.get(f"/api/works/{pending_id}").json()
            assert detail["title"] == "新翻译标题"
        finally:
            asyncio.run(async_client.aclose())


def test_subtitlecat_fetcher_close_and_context_manager(tmp_path: Path) -> None:
    closed = False

    class TrackingClient(httpx.Client):
        def close(self) -> None:
            nonlocal closed
            closed = True
            super().close()

    tracking = TrackingClient(transport=httpx.MockTransport(lambda _req: httpx.Response(404)))
    with SubtitleCatFetcher(cache_path=tmp_path / "misses.json", client=tracking) as fetcher:
        assert fetcher.client is tracking
    assert closed is True
    assert fetcher.client is None


def test_catalog_people_lookup_cards_and_collaborators(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'catalog_opt.db'}")

    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="collab1",
                    code="SONE-601",
                    title="Collab One",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    actors=("三上悠亜", "橋本ありな"),
                ),
                overwrite=True,
            )
            repo.upsert_provider_record(
                ProviderRecord(
                    provider="javdb",
                    external_id="collab2",
                    code="SONE-602",
                    title="Solo Two",
                    family=ContentFamily.JAV,
                    category=MediaCategory.JAPAN,
                    actors=("三上悠亜",),
                ),
                overwrite=True,
            )

        cards = client.get("/api/catalog/people-cards", params={"q": "三上悠亜"}).json()
        assert len(cards["items"]) == 1
        assert cards["items"][0]["work_count"] == 2
        person_id = cards["items"][0]["id"]

        labels = client.get("/api/catalog/people-cards", params=[("id", person_id)]).json()
        assert labels["labels"][person_id] == "三上悠亜"

        detail = client.get(f"/api/catalog/people/{person_id}").json()
        assert len(detail["work_ids"]) == 2
        assert len(detail["collaborators"]) == 1
        assert detail["collaborators"][0]["name"] == "橋本ありな"
        assert detail["collaborators"][0]["count"] == 1

        with app.state.runtime.database.session() as session:
            found = lookup_catalog_person(session, person_id)
            assert found is not None
            assert found.name == "三上悠亜"


def test_library_search_prepared_ranking_and_single_char_actor_guard() -> None:
    no_false_positive = score_work_fields(
        "愛している",
        work_id="w-1",
        title="別タイトル",
        original_title=None,
        primary_code="SONE-701",
        actors=["愛"],
        studio=None,
        series=None,
        tags=[],
    )
    assert no_false_positive is None

    rows: list[dict[str, object]] = [
        {"id": "w-2", "title": "Other", "primary_code": "SONE-702", "actors": ["河北彩伽"]},
        {"id": "w-3", "title": "Code Exact", "primary_code": "SONE-701", "actors": []},
    ]
    page, total = rank_work_dicts("sone701", rows)
    assert total == 1
    assert page[0]["id"] == "w-3"
