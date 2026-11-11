"""One tag tree for grouping, subtree filters, statistics and identity-preserving edits."""

import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.models import Work
from shadow_mdc.services.tag_dictionary import TagDictionaryStore
from shadow_mdc.tag_models import TagDefinition, TagRegistry
from shadow_mdc.tags import use_registry, work_matches_tags


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'tags.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST_TOKEN")
    with TestClient(app) as client:
        yield client


def create(client: TestClient, name: str, parent: str | None = None) -> str:
    registry = app.state.runtime.tag_dictionary.registry()
    existing = registry.resolve(name)
    if existing is not None and existing.id in registry.ids:
        response = client.put(
            "/api/catalog/tags",
            json={
                "tag_id": existing.id,
                "name": name,
                "revision": existing.revision,
                "parent_id": parent,
            },
        )
        assert response.status_code == 200, response.text
        return str(existing.id)
    response = client.post("/api/catalog/tags", json={"name": name, "parent_id": parent})
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


def test_tree_filters_count_distinct_works_and_keep_direct_assignments(client: TestClient) -> None:
    clothing = create(client, "服饰")
    stockings = create(client, "丝袜", clothing)
    black = create(client, "黑丝", stockings)
    white = create(client, "白丝", stockings)
    grey = create(client, "灰丝", stockings)
    original = {
        "a": ["黑丝"],
        "b": [white],
        "c": [black, "白丝", "黑丝"],
        "d": [stockings],
        "e": [grey],
        "f": ["forum"],
    }
    with app.state.runtime.database.session() as session:
        session.add_all([Work(id=identity, title=identity, tags=tags) for identity, tags in original.items()])

    def query(filters: dict[str, object]) -> set[str]:
        response = client.post("/api/catalog/works/query", json=filters)
        assert response.status_code == 200, response.text
        return {work["id"] for work in response.json()["items"]}

    assert query({"tags": [clothing]}) == set("abcde")
    assert query({"tags": ["丝袜"]}) == set("abcde")
    assert query({"tags": [black]}) == set("ac")
    assert query({"tags": [black, white], "tags_all": True}) == {"c"}
    assert query({"tags": [black, white], "tags_all": False}) == set("abc")
    assert query({"tags": [stockings, black], "tags_all": True}) == set("ac")
    assert query({"exclude_tags": [stockings]}) == {"f"}
    facets = client.post("/api/catalog/facets", json={}).json()["tags"]
    by_id = {tag["id"]: tag for tag in facets}
    assert by_id[stockings]["count"] == by_id[clothing]["count"] == 5
    assert by_id[black]["count"] == by_id[white]["count"] == 2
    assert by_id[black]["parent_id"] == stockings
    assert [node["id"] for node in by_id[black]["path"]] == [clothing, stockings, black]
    current = app.state.runtime.tag_dictionary.registry().resolve(black)
    assert (
        client.put(
            "/api/catalog/tags",
            json={
                "tag_id": black,
                "name": "黑丝",
                "revision": current.revision,
                "hidden": True,
            },
        ).status_code
        == 200
    )
    hidden_facets = {tag["id"]: tag for tag in client.post("/api/catalog/facets", json={}).json()["tags"]}
    assert black not in hidden_facets and hidden_facets[stockings]["count"] == 5
    assert query({"tags": [stockings]}) == set("abcde")
    term = next(tag for tag in client.get("/api/catalog/tags").json()["terms"] if tag["id"] == stockings)
    assert term["count"] == 5 and term["direct_count"] == 1
    cards = {tag["id"]: tag for tag in client.get("/api/categories").json()["categories"]}
    assert cards[stockings]["work_count"] == 5 and cards[stockings]["parent_id"] == clothing
    assert client.get("/api/works", params={"tag": "丝袜"}).status_code == 200
    with use_registry(app.state.runtime.tag_dictionary.registry()):
        assert work_matches_tags([black], [stockings])
        assert not work_matches_tags([stockings], [black])
    playlist = client.post(
        "/api/catalog/playlists",
        headers={"X-Catalog-Owner": "fixture"},
        json={
            "name": "Stockings",
            "filters": {"tags": [stockings]},
        },
    ).json()
    moved = client.put(
        "/api/catalog/tags",
        json={
            "tag_id": grey,
            "name": "灰丝",
            "revision": 1,
            "parent_id": clothing,
        },
    )
    assert moved.status_code == 200, moved.text
    assert query({"tags": [stockings]}) == set("abcd")
    members = client.get(
        f"/api/catalog/playlists/{playlist['id']}/works", headers={"X-Catalog-Owner": "fixture"}
    ).json()
    assert {item["id"] for item in members} == set("abcd")
    renamed = client.put(
        "/api/catalog/tags",
        json={
            "tag_id": stockings,
            "name": "袜类",
            "revision": app.state.runtime.tag_dictionary.registry().resolve(stockings).revision,
        },
    )
    assert renamed.status_code == 200 and renamed.json()["parent_id"] == clothing
    assert query({"tags": ["丝袜"]}) == set("abcd")
    assert (
        client.put(
            "/api/catalog/tags",
            json={
                "tag_id": white,
                "name": "白丝",
                "revision": 1,
                "parent_id": None,
            },
        ).status_code
        == 200
    )
    assert query({"tags": [stockings]}) == set("acd")
    with app.state.runtime.database.session() as session:
        assert {identity: session.get(Work, identity).tags for identity in original} == original


def test_invalid_parent_cycles_and_stale_edits_never_write_the_dictionary(client: TestClient) -> None:
    root = create(client, "大节点")
    child = create(client, "子节点", root)
    path = app.state.runtime.tag_dictionary.path
    before = path.read_bytes()
    for parent in (root, child, "tag:missing"):
        response = client.put(
            "/api/catalog/tags",
            json={
                "tag_id": root,
                "name": "大节点",
                "revision": 1,
                "parent_id": parent,
            },
        )
        assert response.status_code == 409, response.text
        assert path.read_bytes() == before
    assert (
        client.post("/api/catalog/tags", json={"name": "无效节点", "parent_id": "tag:missing"}).status_code
        == 409
    )
    assert path.read_bytes() == before
    assert (
        client.put(
            "/api/catalog/tags",
            json={
                "tag_id": child,
                "name": "子节点",
                "revision": 1,
                "parent_id": None,
            },
        ).status_code
        == 200
    )
    changed = path.read_bytes()
    assert (
        client.put(
            "/api/catalog/tags",
            json={
                "tag_id": child,
                "name": "过期编辑",
                "revision": 1,
                "parent_id": root,
            },
        ).status_code
        == 409
    )
    assert path.read_bytes() == changed


def test_legacy_groups_become_real_nodes_once_and_root_renames_preserve_alias_writes(tmp_path: Path) -> None:
    path = tmp_path / "tag-dictionary.json"
    path.write_text(
        json.dumps(
            {
                "revision": 3,
                "terms": [
                    {"id": "tag:clothing", "name": "服饰", "aliases": [], "revision": 5},
                    {"id": "tag:stockings", "name": "丝袜", "group": "服饰", "revision": 7},
                    {"id": "tag:black", "name": "黑丝", "group": "丝袜", "revision": 2},
                ],
                "rules": {},
            }
        ),
        encoding="utf-8",
    )
    store = TagDictionaryStore(path)
    store.initialize()
    document = store.load()
    assert document.tree_version == 1 and document.revision == 4
    terms = {term.id: term for term in document.terms}
    assert len(terms) == 3
    assert terms["tag:stockings"].parent_id == "tag:clothing" and terms["tag:stockings"].revision == 7
    assert terms["tag:black"].parent_id == "tag:stockings" and terms["tag:black"].revision == 2
    snapshot = path.read_bytes()
    store.initialize()
    assert path.read_bytes() == snapshot
    store.update("tag:stockings", "袜类", None, None, 7)
    created = store.create("白丝", (), False, "丝袜")
    assert created.parent_id == "tag:stockings" and created.group == "袜类"
    assert path.stat().st_mode & 0o077 == 0


def test_unregistered_group_creates_a_node_and_tree_is_available_through_skill(client: TestClient) -> None:
    response = client.post("/api/catalog/tags", json={"name": "示例节点", "group": "示例高层"})
    assert response.status_code == 201
    tree = client.get("/api/catalog/tags/tree").json()["nodes"]
    root = next(node for node in tree if node["name"] == "示例高层")
    assert response.json()["parent_id"] == root["id"] and root["parent_id"] is None
    result = client.post(
        "/api/skill/v1/call",
        headers={
            "Authorization": "Bearer REPLACE_TEST_TOKEN",
            "X-Catalog-Owner": "fixture",
        },
        json={"name": "tags.tree", "arguments": {}},
    )
    assert result.status_code == 200 and result.json()["ok"]
    assert root in result.json()["result"]["nodes"]


def test_too_deep_or_missing_parent_paths_are_rejected() -> None:
    values = tuple(
        TagDefinition(
            id=f"tag:level-{index}",
            name=f"Node {index}",
            parent_id=f"tag:level-{index - 1}" if index else None,
        )
        for index in range(65)
    )
    with pytest.raises(ValueError, match="64"):
        TagRegistry(values)
    with pytest.raises(ValueError, match="Missing parent"):
        TagRegistry(
            (
                TagDefinition("tag:first", "First", parent_id="tag:second"),
                TagDefinition("tag:second", "Second", parent_id="tag:missing"),
            )
        )


def test_cyclic_legacy_group_data_is_not_partially_migrated(tmp_path: Path) -> None:
    path = tmp_path / "tag-dictionary.json"
    path.write_text(
        json.dumps(
            {
                "terms": [
                    {"id": "tag:first", "name": "First", "group": "Second"},
                    {"id": "tag:second", "name": "Second", "group": "First"},
                ],
                "rules": {},
            }
        ),
        encoding="utf-8",
    )
    before = path.read_bytes()
    with pytest.raises(ValueError, match="cycles"):
        TagDictionaryStore(path).initialize()
    assert path.read_bytes() == before


def test_merge_synonyms_preserves_old_ids_filters_children_and_source_assignments(client: TestClient) -> None:
    first = create(client, "合并测试甲")
    second = create(client, "合并测试乙")
    child = create(client, "合并测试下级", first)
    with app.state.runtime.database.session() as session:
        session.add_all(
            [
                Work(id="merge-a", title="A", tags=[first, second]),
                Work(id="merge-b", title="B", tags=["合并测试甲"]),
                Work(id="merge-c", title="C", tags=[child]),
            ]
        )
    response = client.post(
        "/api/skill/v1/call",
        headers={"Authorization": "Bearer REPLACE_TEST_TOKEN", "X-Catalog-Owner": "fixture"},
        json={
            "name": "tags.merge",
            "arguments": {
                "source_id": first,
                "target_id": second,
                "source_revision": 1,
                "target_revision": 1,
            },
        },
    )
    assert response.status_code == 200 and response.json()["ok"], response.text
    registry = app.state.runtime.tag_dictionary.registry()
    assert registry.resolve(first).id == second
    assert registry.resolve("合并测试甲").id == second
    assert registry.resolve(child).parent_id == second
    page = client.post("/api/catalog/works/query", json={"tags": [first]}).json()
    assert {work["id"] for work in page["items"]} == {"merge-a", "merge-b", "merge-c"}
    facets = client.post("/api/catalog/facets", json={"tags": [first]}).json()["tags"]
    target = next(tag for tag in facets if tag["id"] == second)
    assert target["count"] == 3 and first in target["redirect_ids"]
    third = create(client, "合并测试丙")
    assert (
        client.post(
            "/api/catalog/tags/merge",
            json={
                "source_id": second,
                "target_id": third,
                "source_revision": 2,
                "target_revision": 1,
            },
        ).status_code
        == 200
    )
    with app.state.runtime.database.session() as session:
        assert session.get(Work, "merge-a").tags == [first, second]
        assert session.get(Work, "merge-b").tags == ["合并测试甲"]
    store = TagDictionaryStore(app.state.runtime.tag_dictionary.path)
    store.initialize()
    first_node, second_node = store.registry().resolve(first), store.registry().resolve(second)
    assert first_node is not None and second_node is not None
    assert first_node.id == second_node.id == third
    assert set(store.registry().redirect_ids(third)) == {first, second}


def test_invalid_merges_never_write_and_keeps_revision_checks(client: TestClient) -> None:
    root = create(client, "不可合并父")
    child = create(client, "不可合并子", root)
    path = app.state.runtime.tag_dictionary.path
    original = path.read_bytes()
    for source, target, revision, status in [
        (root, root, 1, 409),
        (root, child, 1, 409),
        (child, root, 2, 409),
        ("tag:missing", root, 1, 404),
    ]:
        response = client.post(
            "/api/catalog/tags/merge",
            json={
                "source_id": source,
                "target_id": target,
                "source_revision": revision,
                "target_revision": 1,
            },
        )
        assert response.status_code == status
        assert path.read_bytes() == original
