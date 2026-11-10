"""Stable tag identities survive name changes, legacy inputs and catalog restarts."""

import json
from dataclasses import replace
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc import tags
from shadow_mdc.api import app
from shadow_mdc.db.models import Work
from shadow_mdc.services.category_catalog import CategoryCoverEntry, write_manifest
from shadow_mdc.services.media_server import MediaServerSettings
from shadow_mdc.services.tag_dictionary import TagDictionaryStore
from shadow_mdc.tag_models import TagDefinition, TagRegistry


def test_id_aliases_unknown_values_and_noise() -> None:
    spellings = ["素人", "Amateur", " \uff21\uff2d\uff21\uff34\uff25\uff35\uff32 ", "tag:amateur"]
    assert {tags.tag_id(value) for value in spellings} == {
        "tag:amateur",
    }
    refs = tags.tag_refs(["素人", "Amateur", "jav", "自定义 / Label", "自定义 / label"])
    assert len(refs) == 2
    custom = refs[1]
    assert custom.id.startswith("raw:")
    assert tags.tag_id(custom.id) == custom.id
    assert custom.id != tags.tag_id("自定义 / another")
    assert tags.work_matches_tags(["Amateur", "自定义 / Label"], ["tag:amateur", custom.id])
    assert not tags.work_matches_tags(["Amateur"], ["tag:missing"])
    assert tags.resolve_tag("tag:missing") is None
    assert tags.resolve_tag("raw:%FF") is None
    assert tags.resolve_tag("raw:jav") is None
    assert tags.resolve_tag("raw:") is None
    assert tags.facet_tags([["Custom", "CUSTOM"], ["custom"]], always_include=()) == [("CUSTOM", 2)]


def test_registry_rejects_duplicate_ids_and_ambiguous_aliases() -> None:
    first = TagDefinition("tag:first", "名称", ("alias",))
    with pytest.raises(ValueError, match="Duplicate ID"):
        TagRegistry((first, replace(first, name="其它")))
    with pytest.raises(ValueError, match="Ambiguous"):
        TagRegistry((first, TagDefinition("tag:second", "其它", ("\uff21\uff2c\uff29\uff21\uff33",))))
    with pytest.raises(ValueError, match="Invalid tag ID"):
        TagRegistry((TagDefinition("名称", "名称"),))


@pytest.mark.parametrize("legacy_manifest", [False, True])
def test_renaming_keeps_queries_covers_playlists_and_source_values(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, legacy_manifest: bool,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'tags.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    manifest = write_manifest(tmp_path, [CategoryCoverEntry(
        slug="legacy-cover", label="素人", image_file="legacy.jpg",
    )])
    if legacy_manifest:
        payload = json.loads(manifest.read_text())
        del payload["categories"][0]["tag_id"]
        manifest.write_text(json.dumps(payload))
    owner = {"X-Catalog-Owner": "tag-test"}
    with TestClient(app) as client:
        app.state.runtime.media_server_store.save(MediaServerSettings(media_client_token="REPLACE_TEST"))
        with app.state.runtime.database.session() as session:
            session.add_all([
                Work(id="a", title="A", tags=["素人", "Amateur", "POV", "jav"]),
                Work(id="b", title="B", tags=["\uff21\uff2d\uff21\uff34\uff25\uff35\uff32"]),
                Work(id="c", title="C", tags=["自定义"]),
            ])
        created = client.post("/api/catalog/playlists", headers=owner, json={
            "name": "稳定标签片单", "filters": {"tags": ["tag:amateur"]},
        })
        assert created.status_code == 201, created.text
        playlist_id = created.json()["id"]
        cursor = client.post("/api/catalog/works/query", json={
            "tags": ["tag:amateur"], "limit": 1,
        }).json()["next_cursor"]

    store = TagDictionaryStore(tmp_path / "tag-dictionary.json")
    store.update("tag:amateur", "爱好者", aliases=None, hidden=None, revision=1)
    tags._display_chips_cached.cache_clear()
    try:
        with TestClient(app) as client:
            for selected in ["tag:amateur", "素人", "Amateur", "爱好者"]:
                page = client.post("/api/catalog/works/query", json={"tags": [selected]}).json()
                assert page["total"] == 2
                card = next(item for item in page["items"] if item["id"] == "a")
                assert set(card["tags"]) == {"爱好者", "POV"}
                assert any(tag["id"] == "tag:amateur" for tag in card["tag_refs"])
            assert client.post("/api/catalog/works/query", json={
                "tags": ["tag:amateur"], "limit": 1, "cursor": cursor,
            }).json()["total"] == 2
            assert client.post("/api/catalog/works/query", json={
                "tags": ["tag:amateur", "POV"], "tags_all": True,
            }).json()["total"] == 1
            assert client.post("/api/catalog/works/query", json={
                "exclude_tags": ["tag:amateur"],
            }).json()["total"] == 1
            facets = client.post("/api/catalog/facets", json={}).json()["tags"]
            facet = next(tag for tag in facets if tag["id"] == "tag:amateur")
            assert (facet["name"], facet["count"]) == ("爱好者", 2)
            assert "素人" in facet["aliases"]
            empty = client.post("/api/catalog/facets", json={
                "tags": ["tag:amateur"], "q": "no-match",
            }).json()["tags"]
            assert empty[0]["name"] == "爱好者" and empty[0]["count"] == 0
            categories = client.get("/api/categories").json()["categories"]
            category = next(tag for tag in categories if tag["id"] == "tag:amateur")
            assert (category["label"], category["work_count"]) == ("爱好者", 2)
            assert category["slug"] == "legacy-cover"
            assert category["image_url"] == "/api/category-covers/legacy.jpg"
            work = client.get("/api/works/a").json()
            assert work["tags"] == ["素人", "Amateur", "POV", "jav"]
            assert "爱好者" in work["display_tags"]
            assert any(tag["id"] == "tag:amateur" for tag in work["tag_refs"])
            assert client.get("/api/works/tags").json()["tags"][0]["id"] == "tag:amateur"
            mobile = {**owner, "Authorization": "Bearer REPLACE_TEST"}
            mobile_facets = client.post("/api/media/v1/catalog/facets", headers=mobile, json={}).json()
            assert mobile_facets["tags"][0]["id"] == "tag:amateur"
            playlists = client.get("/api/catalog/playlists", headers=owner).json()
            playlist = next(item for item in playlists if item["id"] == playlist_id)
            assert playlist["filters"]["tags"] == ["tag:amateur"]
            assert set(playlist["work_ids"]) == {"a", "b"}
    finally:
        tags._display_chips_cached.cache_clear()
