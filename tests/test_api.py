import json
from dataclasses import dataclass, replace
from pathlib import Path
from subprocess import CompletedProcess

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Repository
from shadow_mdc.domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.base import ProviderRegistry


@dataclass(frozen=True)
class LookupProvider:
    record: ProviderRecord

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self.record.provider,
            name="Lookup fixture",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        return [self.record]


def test_work_and_actor_apis_expose_cached_images_and_relations(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'images.db'}")
    image_url = "https://images.example/cover.jpg"
    record = ProviderRecord(
        provider="fixture",
        external_id="sone00118",
        code="SONE-118",
        title="Image fixture",
        family=ContentFamily.JAV,
        actors=("演员甲", "演员乙"),
        artwork=(Artwork.model_validate({"url": image_url, "kind": "thumb"}),),
    )

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        work = client.post("/api/works/lookup", json={"code": "SONE-118"}).json()["work"]
        work_id = work["id"]
        cached = data_dir / "artwork" / work_id / "poster.jpg"
        cached.parent.mkdir(parents=True)
        cached.write_bytes(b"\xff\xd8fixture")
        with app.state.runtime.database.session() as session:
            repo = Repository(session)
            stored_work = repo.get_work(work_id)
            assert stored_work is not None
            repo.update_artwork_local_paths(stored_work, {image_url: str(cached)})

        works = client.get("/api/works").json()
        actors = client.get("/api/actors").json()
        image = client.get(f"/api/works/{work_id}/artwork/poster")

        assert works[0]["image_url"] == f"/api/works/{work_id}/artwork/poster"
        assert {actor["name"] for actor in works[0]["actor_entities"]} == {
            "演员甲",
            "演员乙",
        }
        assert {actor["name"] for actor in actors} == {"演员甲", "演员乙"}
        assert all(actor["image_url"] == works[0]["image_url"] for actor in actors)
        assert all(actor["works"][0]["image_url"] == works[0]["image_url"] for actor in actors)
        assert image.status_code == 200
        assert image.content == b"\xff\xd8fixture"


def test_library_scan_api_smoke(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "SSIS-123.mp4").write_bytes(b"not-a-real-video")

    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        health = client.get("/api/health")
        assert health.status_code == 200
        assert health.json()["status"] == "ok"

        created = client.post(
            "/api/libraries",
            json={"name": "Fixture", "root_path": str(media_dir), "recursive": True},
        )
        assert created.status_code == 201
        assert created.json()["category"] == "Other"
        library_id = created.json()["id"]

        scan = client.post(f"/api/libraries/{library_id}/scan")
        assert scan.status_code == 200
        assert scan.json()["discovered"] == 1
        tasks = client.get("/api/tasks").json()
        assert tasks[0]["kind"] == "scan"
        assert tasks[0]["status"] == "succeeded"
        assert tasks[0]["summary"]["discovered"] == 1

        assets = client.get("/api/assets")
        assert assets.status_code == 200
        item = assets.json()[0]
        assert item["hints"]["code"] == "SSIS-123"
        assert item["hints"]["mode"] == "code"


def test_non_jav_local_video_requires_screenshot_but_strm_does_not(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "incoming" / "CreatorOne"
    target_dir = tmp_path / "organized"
    media_dir.mkdir(parents=True)
    target_dir.mkdir()
    video = media_dir / "1.mp4"
    pointer = media_dir / "2.strm"
    video.write_bytes(b"fixture video")
    pointer.write_text("https://media.example/creator-one-2.mp4", encoding="utf-8")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'strict-non-jav.db'}")

    def fake_ffmpeg_run(args: list[str], **_: object) -> CompletedProcess[str]:
        Path(args[-1]).write_bytes(b"\xff\xd8generated-frame")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("shadow_mdc.media.screenshots.shutil.which", lambda command: "ffmpeg")
    monkeypatch.setattr("shadow_mdc.media.screenshots.subprocess.run", fake_ffmpeg_run)

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "Strict non-JAV", "root_path": str(media_dir.parent)},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")
        assets = client.get("/api/assets").json()
        assigned = client.post(
            f"/api/assets/{assets[0]['id']}/directory-actor",
            json={"actor": "CreatorOne", "category": "Europe"},
        )
        assert assigned.status_code == 200
        identified = client.get("/api/assets").json()
        video_asset = next(item for item in identified if item["path"].endswith("1.mp4"))
        strm_asset = next(item for item in identified if item["path"].endswith("2.strm"))
        organize_payload = {"mode": "move", "target_root": str(target_dir)}

        blocked = client.post(
            f"/api/assets/{video_asset['id']}/organize/plan",
            json=organize_payload,
        )
        exempt = client.post(
            f"/api/assets/{strm_asset['id']}/organize/plan",
            json=organize_payload,
        )
        before_batch = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json=organize_payload,
        ).json()

        assert blocked.status_code == 422
        assert "local screenshot" in blocked.text
        assert exempt.status_code == 200
        assert before_batch["asset_count"] == 1

        screenshots = client.post(
            f"/api/libraries/{library['id']}/screenshots",
            json={"limit": 10},
        )
        assert screenshots.status_code == 200
        assert screenshots.json() == {
            "attempted": 1,
            "generated": 1,
            "skipped_strm": 1,
            "skipped_cached": 0,
            "skipped_untrusted": 0,
            "failed": 0,
            "errors": [],
        }
        cached = client.post(
            f"/api/libraries/{library['id']}/screenshots",
            json={"limit": 10},
        ).json()
        assert cached["generated"] == 0
        assert cached["skipped_cached"] == 1
        assert cached["skipped_strm"] == 1
        assert client.post(
            f"/api/assets/{video_asset['id']}/organize/plan",
            json=organize_payload,
        ).status_code == 200
        after_batch = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json=organize_payload,
        ).json()
        assert after_batch["asset_count"] == 2
        work = next(
            item for item in client.get("/api/works").json() if item["id"] == video_asset["work_id"]
        )
        generated = [item for item in work["artwork"] if item.get("source") == "local-screenshot"]
        assert {item["kind"] for item in generated} == {"fanart", "poster"}
        assert all(item["asset_id"] == video_asset["id"] for item in generated)


def test_library_bulk_identify_queries_each_code_once(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    (media_dir / "SONE-118A.strm").write_text("https://media.example/a.mp4", encoding="utf-8")
    (media_dir / "SONE-118B.strm").write_text("https://media.example/b.mp4", encoding="utf-8")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'bulk.db'}")
    record = ProviderRecord(
        provider="bulk-fixture",
        external_id="sone00118",
        code="SONE-118",
        title="Verified metadata title",
        family=ContentFamily.JAV,
    )

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        library = client.post(
            "/api/libraries",
            json={"name": "JAV", "root_path": str(media_dir)},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")

        response = client.post(f"/api/libraries/{library['id']}/identify", json={"limit": 100})

        assert response.status_code == 200
        assert response.json() == {
            "queried_identities": 1,
            "code_queries": 1,
            "title_queries": 0,
            "attempted_assets": 2,
            "identified": 2,
            "online_identified": 2,
            "catalog_reused": 0,
            "local_optimized": 0,
            "unresolved": 0,
            "provider_failures": 0,
            "remaining_identities": 0,
            "scope_skipped": 0,
        }
        assert {item["state"] for item in client.get("/api/assets").json()} == {"identified"}
        works = client.get("/api/works").json()
        assert len(works) == 1
        assert works[0]["title"] == "Verified metadata title"

        (media_dir / "SONE-118C.strm").write_text(
            "https://media.example/c.mp4",
            encoding="utf-8",
        )
        client.post(f"/api/libraries/{library['id']}/scan")
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([]),
        )
        reused = client.post(f"/api/libraries/{library['id']}/identify", json={"limit": 100})

        assert reused.status_code == 200
        assert reused.json()["catalog_reused"] == 0
        assert reused.json()["online_identified"] == 0
        assert {item["state"] for item in client.get("/api/assets").json()} == {"identified"}


def test_each_library_can_filter_bulk_identification_to_jav(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    (media_dir / "国产" / "探花合集").mkdir(parents=True)
    (media_dir / "SONE-118.strm").write_text("https://media.example/jav", encoding="utf-8")
    (media_dir / "国产" / "探花合集" / "001.strm").write_text(
        "https://media.example/chinese",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'jav-only.db'}")
    record = ProviderRecord(
        provider="jav-only-fixture",
        external_id="sone00118",
        code="SONE-118",
        title="JAV only title",
        family=ContentFamily.JAV,
    )

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        created = client.post(
            "/api/libraries",
            json={
                "name": "Mixed",
                "root_path": str(media_dir),
                "recognition_scope": "jav_only",
            },
        )
        assert created.status_code == 201
        library = created.json()
        assert library["recognition_scope"] == "jav_only"
        client.post(f"/api/libraries/{library['id']}/scan")

        identified = client.post(
            f"/api/libraries/{library['id']}/identify",
            json={"limit": 100},
        )
        assert identified.status_code == 200
        assert identified.json()["attempted_assets"] == 1
        assert identified.json()["scope_skipped"] == 1
        assert identified.json()["online_identified"] == 1
        assets = client.get("/api/assets").json()
        assert next(item for item in assets if item["hints"]["family"] == "jav")["state"] == "identified"
        chinese = next(item for item in assets if item["hints"]["family"] == "chinese")
        assert chinese["state"] != "identified"
        local_candidate = client.get(f"/api/assets/{chinese['id']}/candidates").json()[0]
        assert client.post(f"/api/candidates/{local_candidate['id']}/accept").status_code == 200
        jav_only_plan = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "sidecar"},
        )
        assert jav_only_plan.status_code == 200
        assert jav_only_plan.json()["asset_count"] == 1

        target_dir = tmp_path / "organized"
        target_dir.mkdir()
        strict_move_plan = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "move", "target_root": str(target_dir)},
        )
        assert strict_move_plan.status_code == 200
        assert strict_move_plan.json()["asset_count"] == 0

        updated = client.patch(
            f"/api/libraries/{library['id']}",
            json={"recognition_scope": "all"},
        )
        assert updated.status_code == 200
        assert updated.json()["recognition_scope"] == "all"


def test_library_identify_uses_local_optimization_only_for_assets_without_code(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "China"
    creator_dir = media_dir / "Dragon's (@DragonLLLLL) 推特合集"
    creator_dir.mkdir(parents=True)
    (creator_dir / "V (1).strm").write_text("https://media.example/one.mp4", encoding="utf-8")
    (media_dir / "SONE-118.strm").write_text(
        "https://media.example/sone-118.mp4",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'local.db'}")

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([]),
        )
        library = client.post(
            "/api/libraries",
            json={"name": "国产", "root_path": str(media_dir)},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")

        result = client.post(f"/api/libraries/{library['id']}/identify", json={"limit": 100})

        assert result.status_code == 200
        assert result.json() == {
            "queried_identities": 2,
            "code_queries": 1,
            "title_queries": 1,
            "attempted_assets": 2,
            "identified": 1,
            "online_identified": 0,
            "catalog_reused": 0,
            "local_optimized": 1,
            "unresolved": 1,
            "provider_failures": 0,
            "remaining_identities": 0,
            "scope_skipped": 0,
        }
        assets = client.get("/api/assets").json()
        states_by_code = {item["hints"]["code"]: item["state"] for item in assets}
        assert states_by_code[None] == "identified"
        assert states_by_code["SONE-118"] != "identified"
        works = client.get("/api/works").json()
        assert [(item["title"], item["category"]) for item in works] == [("DragonLLLLL_1", "China")]


def test_identity_alias_settings_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")
    payload = {
        "studios": {"xb": "杏吧传媒"},
        "series": {"小宝": "小宝探花"},
        "actors": {"演员别名": "规范姓名"},
    }

    with TestClient(app) as client:
        defaults = client.get("/api/settings/identity-aliases")
        assert defaults.status_code == 200
        assert "小宝探花" in defaults.json()["series"].values()

        saved = client.put("/api/settings/identity-aliases", json=payload)
        assert saved.status_code == 200
        assert saved.json() == payload
        assert client.get("/api/settings/identity-aliases").json() == payload

    assert (data_dir / "identity-aliases.json").is_file()


def test_filter_words_settings_round_trip(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        defaults = client.get("/api/settings/filter-words")
        assert defaults.status_code == 200
        assert "社 區 最 新 情 報" in defaults.json()["words"]
        assert "sample" in defaults.json()["words"]

        saved = client.put(
            "/api/settings/filter-words",
            json={"words": ["自定义广告", "自定义广告", " trailer "]},
        )
        assert saved.status_code == 200
        assert saved.json() == {"words": ["自定义广告", "trailer"]}
        assert client.get("/api/settings/filter-words").json() == saved.json()

    assert (data_dir / "filter-words.txt").read_text(encoding="utf-8") == "自定义广告\ntrailer\n"


def test_no_code_asset_can_create_and_accept_local_candidate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media" / "小宝探花"
    media_dir.mkdir(parents=True)
    (media_dir / "01.strm").write_text(
        "https://media.example/本地建档标题.mp4?token=secret",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "No code", "root_path": str(media_dir.parent), "recursive": True},
        )
        assert library.status_code == 201
        scan = client.post(f"/api/libraries/{library.json()['id']}/scan")
        assert scan.status_code == 200
        asset = client.get("/api/assets").json()[0]

        created = client.post(
            f"/api/assets/{asset['id']}/manual-candidate",
            json={"studio": "本地片商", "actors": ["人物甲"], "tags": ["自定义"]},
        )
        assert created.status_code == 201
        candidate = created.json()
        assert candidate["provider"] == "local-manual"
        assert candidate["decision"] == "review"
        assert candidate["record"]["title"] == "本地建档标题"
        assert candidate["record"]["studio"] == "本地片商"

        accepted = client.post(f"/api/candidates/{candidate['id']}/accept")
        assert accepted.status_code == 200
        assert accepted.json()["title"] == "本地建档标题"
        assert accepted.json()["actors"] == ["人物甲"]

        missing = client.post("/api/assets/missing/manual-candidate", json={"title": "Title"})
        assert missing.status_code == 404


def test_library_sidecar_batch_preview_and_apply(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    source = media_dir / "SONE-118.strm"
    source.write_text("https://media.example/SONE-118.mp4", encoding="utf-8")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api.db'}")

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "JAV", "root_path": str(media_dir), "category": "Japan"},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")
        asset = client.get("/api/assets").json()[0]
        candidate = client.get(f"/api/assets/{asset['id']}/candidates").json()[0]
        accepted = client.post(f"/api/candidates/{candidate['id']}/accept")
        assert accepted.status_code == 200

        preview = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "sidecar"},
        )
        assert preview.status_code == 200
        plan = preview.json()
        assert plan["asset_count"] == 1
        assert plan["samples"][0]["operations"][0]["destination"].endswith("movie.nfo")

        applied = client.post(
            f"/api/libraries/{library['id']}/organize/apply",
            json={"mode": "sidecar", "token": plan["token"], "nfo_policy": "replace"},
        )
        assert applied.status_code == 200
        assert applied.json()["succeeded"] == 1
        assert "<uniqueid" in (source.parent / "movie.nfo").read_text(encoding="utf-8")


def test_non_jav_actor_management_image_and_directory_assignment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    source_dir = media_dir / "incoming"
    source_dir.mkdir(parents=True)
    (source_dir / "1.mp4").write_bytes(b"one")
    (source_dir / "2.mp4").write_bytes(b"two")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv(
        "SHADOW_MDC_DATABASE_URL",
        f"sqlite:///{data_dir / 'actor-management.db'}",
    )

    with TestClient(app) as client:
        created_actor = client.post(
            "/api/non-jav-actors",
            json={
                "name": "Creator One",
                "aliases": ["creator_one"],
                "groups": ["independent"],
                "categories": ["Europe"],
                "biography": "Profile text",
                "notes": "Local note",
            },
        )
        assert created_actor.status_code == 201
        assert created_actor.json()["match_names"] == ["Creator One", "creator_one"]

        uploaded = client.post(
            "/api/non-jav-actors/Creator%20One/image",
            content=b"\x89PNG\r\n\x1a\nfixture",
            headers={"Content-Type": "image/png"},
        )
        assert uploaded.status_code == 200
        image_url = uploaded.json()["image_url"]
        assert image_url
        assert client.get(image_url).content == b"\x89PNG\r\n\x1a\nfixture"

        library = client.post(
            "/api/libraries",
            json={"name": "Directory actor", "root_path": str(media_dir)},
        ).json()
        scan = client.post(f"/api/libraries/{library['id']}/scan").json()
        assert scan["queued"] == 2
        compact_inbox = client.get("/api/inbox")
        assert compact_inbox.status_code == 200
        assert len(compact_inbox.json()) == 2
        assert set(compact_inbox.json()[0]) == {
            "id",
            "library_id",
            "path",
            "state",
            "hints",
            "media_info",
        }
        asset = client.get("/api/assets").json()[0]
        assigned = client.post(
            f"/api/assets/{asset['id']}/directory-actor",
            json={"actor": "Creator One", "category": "Europe"},
        )
        assert assigned.status_code == 200
        assert assigned.json()["cataloged"] == 2
        assert client.get("/api/inbox").json() == []
        assert all(item["state"] == "identified" for item in client.get("/api/assets").json())
        works = client.get("/api/works").json()
        assert {work["title"] for work in works} == {"Creator One_1", "Creator One_2"}
        assert all(work["actors"] == ["Creator One"] for work in works)

        renamed = client.patch(
            "/api/non-jav-actors/Creator%20One",
            json={
                "name": "Creator Prime",
                "aliases": ["Creator One", "creator_one"],
                "groups": ["independent"],
                "categories": ["Europe"],
                "biography": "Updated profile",
                "notes": "Edited in UI",
            },
        )
        assert renamed.status_code == 200
        assert renamed.json()["name"] == "Creator Prime"
        assert renamed.json()["image_url"] == image_url
        saved_rules = (data_dir / "directory-actors.json").read_text(encoding="utf-8")
        assert "Creator Prime" in saved_rules


def test_directory_actor_can_bind_an_ancestor_and_preserve_child_hierarchy(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_root = tmp_path / "media"
    actor_root = media_root / "国产" / "探花" / "江南第一深情"
    old_scene = actor_root / "旧版" / "短 黑t妹妹"
    new_scene = actor_root / "新版" / "长裙妹妹"
    old_scene.mkdir(parents=True)
    new_scene.mkdir(parents=True)
    (old_scene / "IMG_0940.strm").write_text("https://media.example/old.mp4", encoding="utf-8")
    (new_scene / "v1.strm").write_text("https://media.example/new.mp4", encoding="utf-8")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'hierarchy.db'}")

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "Hierarchy", "root_path": str(media_root)},
        ).json()
        assert client.post(f"/api/libraries/{library['id']}/scan").status_code == 200
        asset = next(
            item for item in client.get("/api/assets").json() if item["path"].endswith("IMG_0940.strm")
        )
        leaf_assigned = client.post(
            f"/api/assets/{asset['id']}/directory-actor",
            json={"actor": "短黑妹妹", "category": "China"},
        )
        assert leaf_assigned.status_code == 200
        assert leaf_assigned.json()["cataloged"] == 1
        assigned = client.post(
            f"/api/assets/{asset['id']}/directory-actor",
            json={
                "actor": "江南第一深情",
                "category": "China",
                "directory": str(actor_root),
            },
        )

        assert assigned.status_code == 200
        assert assigned.json()["directory"] == str(actor_root.resolve())
        assert assigned.json()["matched_assets"] == 2
        assert assigned.json()["cataloged"] == 2
        works = {item["title"]: item for item in client.get("/api/works").json()}
        assert set(works) == {
            "江南第一深情-旧版-短黑t妹妹-IMG_0940",
            "江南第一深情-新版-长裙妹妹-v1",
        }
        old = works["江南第一深情-旧版-短黑t妹妹-IMG_0940"]
        assert old["actors"] == ["江南第一深情"]
        assert {"探花", "旧版", "短黑t妹妹"}.issubset(old["tags"])
        saved_rules = json.loads((data_dir / "directory-actors.json").read_text(encoding="utf-8"))
        assert saved_rules["rules"][0]["directory"] == str(actor_root.resolve())


def test_move_batch_plans_and_cleans_empty_source_tree_with_filtered_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "incoming"
    source_dir = media_dir / "batch"
    target_dir = tmp_path / "organized"
    (source_dir / "empty-child").mkdir(parents=True)
    (source_dir / "ads").mkdir()
    target_dir.mkdir()
    source = source_dir / "SONE-118.strm"
    source.write_text("https://media.example/SONE-118.mp4", encoding="utf-8")
    (source_dir / "广告片.txt").write_text("junk", encoding="utf-8")
    (source_dir / "ads" / "广告片.url").write_text("junk", encoding="utf-8")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'move-clean.db'}")

    with TestClient(app) as client:
        saved_filter = client.put(
            "/api/settings/filter-words",
            json={"words": ["广告片"]},
        )
        assert saved_filter.status_code == 200
        library = client.post(
            "/api/libraries",
            json={"name": "Move clean", "root_path": str(media_dir)},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")
        asset = client.get("/api/assets").json()[0]
        candidate = client.get(f"/api/assets/{asset['id']}/candidates").json()[0]
        assert client.post(f"/api/candidates/{candidate['id']}/accept").status_code == 200

        preview = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "move", "target_root": str(target_dir)},
        )
        assert preview.status_code == 200
        plan = preview.json()
        operation_kinds = {
            operation["kind"] for sample in plan["samples"] for operation in sample["operations"]
        }
        assert "delete_filtered_file" in operation_kinds
        assert "remove_directory" in operation_kinds

        applied = client.post(
            f"/api/libraries/{library['id']}/organize/apply",
            json={
                "mode": "move",
                "target_root": str(target_dir),
                "token": plan["token"],
                "nfo_policy": "replace",
            },
        )
        assert applied.status_code == 200
        assert applied.json()["succeeded"] == 1
        assert media_dir.is_dir()
        assert not source_dir.exists()
        moved_assets = client.get("/api/assets").json()
        moved_path = Path(moved_assets[0]["path"])
        assert moved_path.is_file()
        assert moved_path.is_relative_to(target_dir)


def test_library_copy_batch_preserves_multiple_stream_variants(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "media"
    target_dir = tmp_path / "organized"
    media_dir.mkdir()
    target_dir.mkdir()
    (media_dir / "SONE-118.strm").write_text("https://media.example/main", encoding="utf-8")
    (media_dir / "SONE-118 (1).strm").write_text(
        "https://media.example/alternate",
        encoding="utf-8",
    )
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'variants.db'}")

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "JAV variants", "root_path": str(media_dir), "category": "Japan"},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")
        assets = client.get("/api/assets").json()
        for asset in assets:
            candidate = client.get(f"/api/assets/{asset['id']}/candidates").json()[0]
            accepted = client.post(f"/api/candidates/{candidate['id']}/accept")
            assert accepted.status_code == 200

        preview = client.post(
            f"/api/libraries/{library['id']}/organize/plan",
            json={"mode": "copy", "target_root": str(target_dir)},
        )
        assert preview.status_code == 200
        plan = preview.json()
        media_destinations = sorted(
            operation["destination"]
            for sample in plan["samples"]
            for operation in sample["operations"]
            if operation["detail"] == "media"
        )
        assert media_destinations[0].endswith("_1.strm")
        assert media_destinations[1].endswith("_2.strm")

        applied = client.post(
            f"/api/libraries/{library['id']}/organize/apply",
            json={
                "mode": "copy",
                "target_root": str(target_dir),
                "token": plan["token"],
                "nfo_policy": "replace",
            },
        )
        assert applied.status_code == 200
        assert applied.json()["succeeded"] == 2
        contents = {path.read_text(encoding="utf-8") for path in target_dir.rglob("*.strm")}
        assert contents == {
            "https://media.example/main",
            "https://media.example/alternate",
        }


def test_lookup_by_number_creates_work_without_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'lookup.db'}")
    record = ProviderRecord(
        provider="lookup-fixture",
        external_id="sone-118",
        code="SONE-118",
        title="Lookup title",
        family=ContentFamily.JAV,
        plot="Lookup plot",
    )

    with TestClient(app) as client:
        app.state.runtime = replace(
            app.state.runtime,
            providers=ProviderRegistry([LookupProvider(record)]),
        )
        diagnostic = client.post("/api/providers/diagnose", json={"code": "SONE-118"})
        assert diagnostic.status_code == 200
        assert diagnostic.json()["diagnostics"][0]["status"] == "success"
        assert diagnostic.json()["diagnostics"][0]["accepted"] == 1
        response = client.post("/api/works/lookup", json={"code": "sone-118"})

        assert response.status_code == 200
        payload = response.json()
        assert payload["matched_records"] == 1
        assert payload["work"]["primary_code"] == "SONE-118"
        assert payload["work"]["plot"] == "Lookup plot"
        assert client.get("/api/assets").json() == []
        assert client.get("/api/tasks").json()[0]["kind"] == "lookup"
