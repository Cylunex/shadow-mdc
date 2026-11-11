"""Vocabulary is runtime data: writable, isolated, durable and identity preserving."""

from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Work
from shadow_mdc.services.tag_dictionary import TagDictionaryStore
from shadow_mdc.tags import normalize_tags, use_registry


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'catalog.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST_TOKEN")
    with TestClient(app) as client:
        client.headers.update({"Authorization": "Bearer REPLACE_TEST_TOKEN", "X-Catalog-Owner": "fixture"})
        with app.state.runtime.database.session() as session:
            session.add(
                Work(id="a", title="A", tags=["Amateur", "素人", "forum", "seed-import", "自定义 / 标记"])
            )
        yield client


def call(client: TestClient, name: str, arguments: dict[str, object]) -> dict[str, object]:
    response = client.post("/api/skill/v1/call", json={"name": name, "arguments": arguments})
    assert response.status_code == 200, response.text
    return dict(response.json())


def test_dictionary_data_rename_keeps_source_values_ids_covers_and_filters(client: TestClient) -> None:
    client.get("/api/categories")  # Warm the response cache before changing display data.
    result = call(client, "tags.update", {"tag_id": "tag:amateur", "name": "新名称", "revision": 1})
    assert result["ok"]
    for value in ("tag:amateur", "Amateur", "素人", "新名称"):
        page = client.post("/api/catalog/works/query", json={"tags": [value]}).json()
        assert page["total"] == 1 and "新名称" in page["items"][0]["tags"]
    categories = client.get("/api/categories").json()["categories"]
    assert next(value for value in categories if value["id"] == "tag:amateur")["label"] == "新名称"
    detail = client.get("/api/works/a").json()
    assert detail["tags"] == ["Amateur", "素人", "forum", "seed-import", "自定义 / 标记"]
    entries = client.get("/api/catalog/tags", params={"q": "Amateur"}).json()["terms"]
    assert entries[0]["name"] == "新名称" and entries[0]["count"] == 1
    assert (
        call(client, "tags.update", {"tag_id": "tag:amateur", "name": "Stale", "revision": 1})["status"]
        == 409
    )
    assert call(client, "tags.create", {"name": "Amateur"})["status"] == 409


def test_discovered_raw_term_can_be_renamed_without_losing_saved_identity(client: TestClient) -> None:
    terms = client.get("/api/catalog/tags", params={"q": "自定义"}).json()["terms"]
    old = terms[0]
    playlist = client.post(
        "/api/catalog/playlists", json={"name": "raw list", "filters": {"tags": [old["id"]]}}
    ).json()
    updated = client.put("/api/catalog/tags", json={"tag_id": old["id"], "name": "新的展示名", "revision": 1})
    assert updated.status_code == 200, updated.text
    assert updated.json()["id"] == old["id"]
    for identity in (old["id"], "自定义 / 标记", "新的展示名"):
        assert client.post("/api/catalog/works/query", json={"tags": [identity]}).json()["total"] == 1
    assert client.get(f"/api/catalog/playlists/{playlist['id']}/works").json()[0]["id"] == "a"


def test_noise_visibility_is_data_and_editing_work_tags_preserves_source_markers(client: TestClient) -> None:
    result = call(client, "works.tags", {"work_id": "a", "add": ["经典", "LTX"]})
    assert result["ok"]
    actual = client.get("/api/works/a").json()
    assert {"Amateur", "素人", "forum", "seed-import", "经典", "LTX"}.issubset(actual["tags"])
    assert "forum" not in actual["display_tags"]
    raw = client.get("/api/catalog/tags", params={"q": "forum"}).json()["terms"][0]
    assert raw["hidden"] is True
    result = call(
        client, "tags.update", {"tag_id": raw["id"], "name": "论坛收录", "hidden": False, "revision": 1}
    )
    assert result["ok"]
    assert "论坛收录" in client.get("/api/works/a").json()["display_tags"]
    assert call(
        client, "tags.update", {"tag_id": raw["id"], "name": "论坛收录", "hidden": True, "revision": 2}
    )["ok"]
    assert "论坛收录" not in client.get("/api/works/a").json()["display_tags"]
    assert "forum" in client.get("/api/works/a").json()["tags"]


def test_rules_are_editable_data_and_have_a_revision_guard(client: TestClient) -> None:
    result = client.get("/api/catalog/tags/rules").json()
    result["rules"]["noise_exact"].append("自定义 / 标记")
    assert call(client, "tags.rules.update", result)["ok"]
    assert "自定义 / 标记" not in client.get("/api/works/a").json()["display_tags"]
    assert call(client, "tags.rules.update", result)["status"] == 409
    assert client.get("/api/catalog/tags/rules").json()["rules"]["noise_exact"][-1] == "自定义 / 标记"


def test_dictionary_survives_initialization_and_is_isolated_by_data_directory(tmp_path: Path) -> None:
    first = TagDictionaryStore(tmp_path / "first/tag-dictionary.json")
    second = TagDictionaryStore(tmp_path / "second/tag-dictionary.json")
    first.initialize()
    second.initialize()
    first.update("tag:amateur", "持久化名称", None, None, 1)
    first.initialize()
    reopened = TagDictionaryStore(first.path)
    reopened.initialize()
    with use_registry(reopened.registry()):
        assert normalize_tags(["Amateur"]) == ["持久化名称"]
    with use_registry(second.registry()):
        assert normalize_tags(["Amateur"]) == ["素人"]
    assert first.path.stat().st_mode & 0o077 == 0


def test_existing_classification_term_writes_are_available_through_skill(client: TestClient) -> None:
    created = call(
        client,
        "classifications.create",
        {"dimension": "forms", "name": "测试题材", "aliases": ["Fixture genre"]},
    )
    assert created["ok"] and isinstance(created["result"], dict)
    term = created["result"]
    assert call(client, "classifications.assign", {"work_id": "a", "forms": [term["id"]]})["ok"]
    assert call(client, "classifications.update", {"term_id": term["id"], "name": "改名题材", "revision": 1})[
        "ok"
    ]
    taxonomy = client.get("/api/catalog/works/a/taxonomy").json()
    assert taxonomy["classification_refs"]["forms"][0]["name"] == "改名题材"
    assert (
        call(client, "classifications.update", {"term_id": term["id"], "name": "stale", "revision": 1})[
            "status"
        ]
        == 409
    )


def test_new_word_is_persisted_and_supports_immediate_assignment(client: TestClient) -> None:
    created = call(client, "tags.create", {"name": "新建词条", "aliases": ["New word"]})
    assert created["ok"] and isinstance(created["result"], dict)
    term = created["result"]
    assert call(client, "works.tags", {"work_id": "a", "add": [term["id"]]})["ok"]
    assert client.post("/api/catalog/works/query", json={"tags": ["New word"]}).json()["total"] == 1
    assert call(client, "tags.update", {"tag_id": term["id"], "name": "新建改名", "revision": 1})["ok"]
    stored = app.state.runtime.tag_dictionary.load()
    assert next(value for value in stored.terms if value.id == term["id"]).name == "新建改名"
    assert "New word" in next(value for value in stored.terms if value.id == term["id"]).aliases


def test_hidden_tag_can_still_be_removed_from_a_work_by_stable_id(client: TestClient) -> None:
    assert call(
        client, "tags.update", {"tag_id": "tag:amateur", "name": "素人", "hidden": True, "revision": 1}
    )["ok"]
    assert call(client, "works.tags", {"work_id": "a", "remove": ["tag:amateur"]})["ok"]
    tags = client.get("/api/works/a").json()["tags"]
    assert "Amateur" not in tags and "素人" not in tags
    assert "forum" in tags and "seed-import" in tags
