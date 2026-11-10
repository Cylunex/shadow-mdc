from datetime import date
from io import BytesIO
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from shadow_mdc.api import app
from shadow_mdc.db.models import Actor, Collection, Work, WorkActor, WorkCollection
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.non_jav_actor_catalog import NonJavActorProfile
from shadow_mdc.services.playback import (
    VodProviderConfig,
    extract_vod_tracks,
    probe_video_url,
    search_vod_provider,
)


def test_unified_catalog_facets_and_actor_intersection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="a", title="One", release_date=date(2015, 1, 1),
                     actors=["A", "B"], tags=["海滩", "剧情"]),
                Work(id="b", title="Two", release_date=date(2020, 1, 1), actors=["A"], tags=["海滩"]),
                Work(id="c", title="Unknown year", actors=[], tags=[]),
                Actor(id="aa", name="A", normalized_name="a"),
                Actor(id="bb", name="B", normalized_name="b"),
                Collection(id="studio", name="Studio", normalized_name="studio", kind="studio"),
            ])
            session.flush()
            session.add_all([
                WorkActor(work_id="a", actor_id="aa"), WorkActor(work_id="a", actor_id="bb"),
                WorkActor(work_id="b", actor_id="aa"),
                WorkCollection(work_id="a", collection_id="studio"),
                WorkCollection(work_id="b", collection_id="studio"),
            ])
        result = client.post("/api/catalog/works/query", json={
            "actors": ["aa", "bb"], "actors_all": True,
        })
        assert result.status_code == 200, result.text
        assert [w["id"] for w in result.json()["items"]] == ["a"]
        facets = client.post("/api/catalog/facets", json={"organizations": ["studio"]})
        assert facets.status_code == 200, facets.text
        assert facets.json()["organizations"][0]["count"] == 2
        assert {f["id"] for f in facets.json()["decades"]} == {"2010", "2020"}
        assert client.post("/api/catalog/works/query", json={"unknown_year": True}).json()["total"] == 1
        first = client.post("/api/catalog/works/query", json={"limit": 1}).json()
        assert client.post("/api/catalog/works/query", json={
            "cursor": first["next_cursor"], "q": "changed",
        }).status_code == 400
        assert client.get("/api/media/v1/catalog/people").status_code == 503
        assert client.put("/api/catalog/works/c/taxonomy", json={
            "genres": ["genres:documentary"], "forms": ["forms:short"],
            "regions": ["regions:jp"], "release_year": 2012,
        }).status_code == 200
        selected = client.post("/api/catalog/works/query", json={
            "genres": ["genres:documentary"], "year_from": 2010,
        })
        assert selected.json()["items"][0]["id"] == "c"
        assert selected.json()["items"][0]["year"] == 2012
        assert client.post("/api/catalog/works/query", json={"unknown_year": True}).json()["total"] == 0
        assert client.post("/api/catalog/facets", json={}).json()["genres"][0]["name"] == "纪录"
        people = client.get("/api/catalog/people").json()
        assert next(p for p in people if p["id"] == "aa")["work_ids"] == ["a", "b"]
        page = client.get("/api/catalog/people-cards", params={"limit": 1}).json()
        assert page["next_offset"] == 1
        assert page["total"] == 2
        assert [(p["id"], p["work_count"]) for p in page["items"]] == [("aa", 2)]
        assert "work_ids" not in page["items"][0]
        second_page = client.get("/api/catalog/people-cards", params={"limit": 1, "offset": 1}).json()
        assert [p["id"] for p in second_page["items"]] == ["bb"]
        assert second_page["next_offset"] is None
        assert second_page["total"] == 2
        beyond = client.get("/api/catalog/people-cards", params={"offset": 100}).json()
        assert beyond["items"] == [] and beyond["total"] == 2
        assert client.get("/api/catalog/people-cards", params={"limit": 101}).status_code == 422


def test_category_counts_and_tag_queries_share_canonical_aliases(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'aliases.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="a", title="A", tags=["素人", "Amateur", " Amateur ", "POV", "jav"]),
                Work(id="b", title="B", tags=["\uff21\uff2d\uff21\uff34\uff25\uff35\uff32"]),
                Work(id="c", title="C", tags=["素人", "主観"]),
                Work(id="d", title="D", tags=["自定义", "jav"]),
            ])
        categories = client.get("/api/categories").json()["categories"]
        assert next(row["work_count"] for row in categories if row["label"] == "素人") == 3
        for selected in ["素人", "Amateur"]:
            first = client.post("/api/catalog/works/query", json={"tags": [selected], "limit": 1}).json()
            assert first["total"] == 3
            second = client.post("/api/catalog/works/query", json={
                "tags": [selected], "limit": 1, "cursor": first["next_cursor"],
            }).json()
            assert first["items"][0]["id"] != second["items"][0]["id"]
        assert client.post("/api/catalog/works/query", json={
            "tags": ["素人", "POV"], "tags_all": True,
        }).json()["total"] == 2
        assert client.post("/api/catalog/works/query", json={
            "tags": ["自定义", "素人"],
        }).json()["total"] == 4
        assert client.post("/api/catalog/works/query", json={
            "exclude_tags": ["素人"],
        }).json()["total"] == 1
        facets = client.post("/api/catalog/facets", json={}).json()["tags"]
        assert {row["name"]: row["count"] for row in facets} == {"素人": 3, "POV": 2, "自定义": 1}


@pytest.mark.asyncio
async def test_online_candidates_require_exact_identity_and_isolate_parts() -> None:
    def response(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={"list": [
            {"vod_id": 1, "type_id": 1, "vod_name": "ABC-1234",
             "vod_play_url": "https://example.com/wrong.m3u8"},
            {"vod_id": 2, "type_id": 1, "vod_name": "ABC-123",
             "vod_play_url": "CD1$https://example.com/one.m3u8#CD2$https://example.com/two.m3u8"},
        ]})
    async with httpx.AsyncClient(transport=httpx.MockTransport(response)) as client:
        sources = await search_vod_provider(VodProviderConfig(
            name="fixture", base_url="https://example.com/api", whitelist_type_ids=[1],
        ), client, code="ABC-123")
    assert len(sources) == 2
    assert sources[0].version_id != sources[1].version_id
    assert all("wrong" not in source.url for source in sources)


def test_persistent_people_merge_undo_and_private_fields(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'people.db'}")
    with TestClient(app) as client:
        first = client.post("/api/catalog/people", json={
            "name": "Same Name", "aliases": ["First Alias"], "notes": "private",
            "match_names": ["private-path"], "revision": 1,
        }).json()
        second = client.post("/api/catalog/people", json={
            "name": "Same Name", "aliases": ["Second Alias"], "revision": 1,
        }).json()
        assert first["id"] != second["id"]
        found = client.get("/api/catalog/people-cards", params={"q": "second alias", "sort": "name"}).json()
        assert [(p["id"], p["same_name"], p["work_count"]) for p in found["items"]] == [
            (second["id"], True, 0),
        ]
        assert found["total"] == 1
        assert client.get("/api/catalog/people-cards", params={"q": "no match"}).json()["total"] == 0
        assert client.put(f"/api/catalog/people/{first['id']}/preference", json={
            "favorite": True, "blacklist": False,
        }).status_code == 200
        followed = client.get("/api/catalog/people-cards", params={"followed": True}).json()
        assert [p["id"] for p in followed["items"]] == [first["id"]]
        assert followed["total"] == 1
        assert len(client.get("/api/catalog/people").json()) == 2
        merged = client.post("/api/catalog/people-merge", json={
            "target_id": first["id"], "source_id": second["id"],
            "target_revision": 1, "source_revision": 1,
        })
        assert merged.status_code == 200, merged.text
        assert len(client.get("/api/catalog/people").json()) == 1
        alias_page = client.get("/api/catalog/people-cards", params={"q": "second alias"}).json()
        assert [p["id"] for p in alias_page["items"]] == [first["id"]]
        labels = client.get("/api/catalog/people-cards", params={"id": second["ref"]}).json()
        assert labels["labels"] == {second["ref"]: first["name"]}
        redirected = client.get("/api/catalog/people/" + second["id"]).json()
        assert redirected["id"] == first["id"]
        assert "Second Alias" in redirected["aliases"]
        stale = client.put("/api/catalog/people/" + first["id"], json={
            "name": "Stale edit", "revision": 1,
        })
        assert stale.status_code == 409
        app.state.runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_TEST"))
        mobile = client.get("/api/media/v1/catalog/people", headers={
            "Authorization": "Bearer REPLACE_TEST",
        }).json()
        assert mobile[0]["notes"] == ""
        assert mobile[0]["match_names"] == []
        undo = "/api/catalog/people-merges/" + merged.json()["merge_id"] + "/undo"
        assert client.post(undo).status_code == 204
        assert client.post(undo).status_code == 204
        assert len(client.get("/api/catalog/people").json()) == 2


def test_ordered_playlists_scopes_revisions_and_organization_cycles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'lists.db'}")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="a", title="A"), Work(id="b", title="B"),
                Collection(id="first", name="One", normalized_name="one", kind="studio"),
                Collection(id="second", name="Two", normalized_name="two", kind="label"),
            ])
        independent = client.post("/api/catalog/organizations", json={"name": "Independent"})
        assert independent.status_code == 201, independent.text
        assert independent.json()["kind"] == "organization"
        assert independent.json()["work_count"] == 0
        assert client.get("/api/collections").status_code == 200
        client.headers["X-Catalog-Owner"] = "device-a"
        created = client.post("/api/catalog/playlists", json={
            "name": "My list", "work_ids": ["b", "a", "b"],
        })
        assert created.status_code == 201, created.text
        playlist = created.json()
        assert playlist["work_ids"] == ["b", "a"]
        path = "/api/catalog/playlists/" + playlist["id"]
        assert [w["id"] for w in client.get(path + "/works").json()] == ["b", "a"]
        update = client.put(path, json={"name": "My list", "work_ids": ["a", "b"], "revision": 1})
        assert update.status_code == 200, update.text
        assert client.put(path, json={"name": "Stale", "work_ids": ["a"], "revision": 1}).status_code == 409
        client.headers["X-Catalog-Owner"] = "device-b"
        assert client.get("/api/catalog/playlists").json() == []
        assert client.get(path + "/works").status_code == 404
        assert client.put(path, json={"name": "Other", "work_ids": [], "revision": 2}).status_code == 404
        assert client.put("/api/catalog/organizations/first", json={
            "name": "One", "parent_id": "second", "revision": 0,
        }).status_code == 200
        assert client.put("/api/catalog/organizations/second", json={
            "name": "Two", "parent_id": "first", "revision": 0,
        }).status_code == 409
        assert client.put("/api/catalog/organizations/first/works", json={
            "work_id": "a", "role": "distributor",
        }).status_code == 200
        queried = client.post("/api/catalog/works/query", json={
            "organizations": ["first"], "organization_role": "distributor",
        })
        assert queried.status_code == 200, queried.text
        assert [work["id"] for work in queried.json()["items"]] == ["a"]


def test_manual_credits_and_renames_keep_the_same_identity(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'credits.db'}")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add(Work(id="work", title="A work"))
        created = client.post("/api/catalog/people", json={"name": "Original", "revision": 1}).json()
        path = "/api/catalog/people/" + created["id"]
        linked = client.put(path + "/works", json={"work_id": "work", "revision": 1})
        assert linked.status_code == 200, linked.text
        assert linked.json()["work_ids"] == ["work"]
        renamed = client.put(path, json={"name": "New name", "revision": 2})
        assert renamed.status_code == 200, renamed.text
        assert renamed.json()["id"] == created["id"]
        assert "Original" in renamed.json()["aliases"]
        assert client.get("/api/catalog/people?work_id=work").json()[0]["name"] == "New name"
        assert client.get("/api/catalog/migration").json()["new_people"] == 0
        assert client.put(path + "/works", json={
            "work_id": "work", "included": False, "revision": 3,
        }).status_code == 200
        assert client.get("/api/catalog/people?work_id=work").json() == []
        assert client.put(path + "/works", json={
            "work_id": "work", "included": True, "role": "director", "revision": 4,
        }).status_code == 200
        assert client.get(path).json()["directed_work_ids"] == ["work"]
        image = BytesIO()
        Image.new("RGB", (8, 8)).save(image, format="PNG")
        uploaded = client.post(path + "/image?revision=5", content=image.getvalue())
        assert uploaded.status_code == 200, uploaded.text
        assert client.get(path + "/image").content == image.getvalue()
        assert client.post(path + "/image?revision=6", content=b"invalid").status_code == 415



@pytest.mark.asyncio
async def test_video_probe_is_bounded_and_track_identity_survives_reorder() -> None:
    first = extract_vod_tracks(
        "CD1$https://example.com/one?sig=old#CD2$https://example.com/two?sig=old",
    )
    second = extract_vod_tracks(
        "CD2$https://example.com/two?sig=new#CD1$https://example.com/one?sig=new",
    )
    assert first[0].identity == second[1].identity
    assert first[1].identity == second[0].identity
    assert first[0].identity != first[1].identity
    async with httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: httpx.Response(206, content=b"\x00\x00\x00\x18ftypisom" + b"0" * 2048),
    )) as client:
        assert (await probe_video_url(client, "https://example.com/file", timeout_seconds=1)).ok


def test_merge_history_can_be_undone_in_reverse_order(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'merge-chain.db'}")
    with TestClient(app) as client:
        created = [client.post("/api/catalog/people", json={
            "name": name, "revision": 1,
        }).json() for name in ("A", "B", "C", "D")]
        histories = []
        for index, source in enumerate(created[1:], start=1):
            response = client.post("/api/catalog/people-merge", json={
                "target_id": created[0]["id"], "source_id": source["id"],
                "target_revision": index, "source_revision": 1,
            })
            assert response.status_code == 200, response.text
            histories.append(response.json()["merge_id"])
        for merge_id in reversed(histories):
            response = client.post("/api/catalog/people-merges/" + merge_id + "/undo")
            assert response.status_code == 204, response.text
        assert len(client.get("/api/catalog/people").json()) == 4


def test_curated_rename_preserves_private_notes_old_ref_and_zero_work_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'legacy.db'}")
    with TestClient(app) as client:
        store = app.state.runtime.non_jav_actor_store
        store.upsert(NonJavActorProfile(name="Original", notes="private", match_names=("directory",)))
        old = client.get("/api/catalog/people").json()[0]
        assert old["notes"] == "private"
        assert old["work_ids"] == []
        changed = client.patch("/api/non-jav-actors/Original", json={
            "name": "Renamed", "notes": "private", "aliases": ["Original"],
        })
        assert changed.status_code == 200, changed.text
        new = client.get("/api/catalog/people").json()
        assert len(new) == 1
        assert new[0]["id"] == old["id"]
        assert client.get("/api/catalog/people/nj:Original").json()["id"] == old["id"]
        assert client.get("/api/catalog/people/nj:Renamed").json()["id"] == old["id"]


def test_dynamic_playlist_reuses_mobile_contract_and_tracks_new_works(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'rules.db'}")
    with TestClient(app) as client:
        runtime = app.state.runtime
        runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_TEST"))
        with runtime.database.session() as session:
            session.add_all([Work(id="first", title="Zulu", tags=["剧情"]), Work(id="other", title="Other")])
        owner = {"X-Catalog-Owner": "same-device-space"}
        mobile = {**owner, "Authorization": "Bearer REPLACE_TEST"}
        created = client.post("/api/catalog/playlists", headers=owner, json={
            "name": "Dynamic",
            "filters": {"tags": ["剧情"], "sort": "title", "limit": 1, "cursor": "ignored"},
        })
        assert created.status_code == 201, created.text
        playlist = created.json()
        assert playlist["work_ids"] == ["first"]
        assert playlist["filters"]["cursor"] is None
        with runtime.database.session() as session:
            session.add(Work(id="added", title="Alpha", tags=["剧情"]))
        path = "/api/media/v1/catalog/playlists/" + playlist["id"] + "/works"
        assert [row["id"] for row in client.get(path, headers=mobile).json()] == ["added", "first"]
        assert client.get(path, headers={**mobile, "X-Catalog-Owner": "other"}).status_code == 404
        mobile_playlists = client.get("/api/media/v1/catalog/playlists", headers=mobile).json()
        assert mobile_playlists[0]["work_ids"] == ["added", "first"]
        assert client.delete("/api/catalog/playlists/" + playlist["id"], headers=owner).status_code == 204
        assert client.get(path, headers=mobile).status_code == 404


def test_web_catalog_uses_existing_artwork_and_legacy_filters(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="a", title="Local thumbnail", category="Japan", primary_code="AAA-001"),
                Work(id="b", title="Remote cover", category="China", primary_code="BBB-002",
                     artwork=[{"kind": "poster", "url": "https://example.com/cover.jpg"}]),
                Work(id="c", title="No cover", category="Europe"),
                Collection(id="series", name="Fixture", normalized_name="fixture", kind="series"),
            ])
            session.flush()
            session.add(WorkCollection(work_id="a", collection_id="series"))
        image_dir = tmp_path / "artwork" / "a"
        image_dir.mkdir(parents=True)
        Image.new("RGB", (8, 8)).save(image_dir / "thumb.jpg")
        app.state.runtime.library_prefs_store.set_want("a", True)
        response = client.post("/api/catalog/works/query", json={})
        assert response.status_code == 200, response.text
        rows = {row["id"]: row for row in response.json()["items"]}
        assert rows["a"]["poster"] == "/api/works/a/artwork/thumb"
        assert client.get(rows["a"]["poster"]).status_code == 200
        assert rows["b"]["poster"] == "https://example.com/cover.jpg"
        assert rows["c"]["poster"] == ""
        assert client.post("/api/catalog/works/query", json={"status": "want"}).json()["total"] == 1
        assert client.post("/api/catalog/works/query", json={"category": "China"}).json()["total"] == 1
        assert client.post("/api/catalog/works/query", json={
            "collection": "series:Fixture",
        }).json()["total"] == 1
        assert client.post("/api/catalog/facets", json={}).json()["collections"] == [
            {"id": "series:Fixture", "name": "Fixture", "count": 1, "role": ""},
        ]
        assert client.post("/api/catalog/works/query", json={"status": "downloaded"}).json()["total"] == 0
        assert client.post("/api/catalog/works/query", json={"status": "undownloaded"}).json()["total"] == 3
        for sort in ("code", "ranked", "random"):
            ids: list[str] = []
            cursor = None
            for _ in range(3):
                page = client.post("/api/catalog/works/query", json={
                    "sort": sort, "seed": 10, "limit": 1, "cursor": cursor,
                })
                assert page.status_code == 200, page.text
                ids.extend(row["id"] for row in page.json()["items"])
                cursor = page.json()["next_cursor"]
            assert len(set(ids)) == 3, (sort, ids)
            assert cursor is None
