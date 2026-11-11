"""Maintenance must preserve identities, edits, and future source enrichment."""

from io import BytesIO
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from shadow_mdc import api as api_module
from shadow_mdc.api import app
from shadow_mdc.catalog_api import catalog_credit_names, read_catalog_people
from shadow_mdc.db.models import Actor, CatalogPersonCredit, Work, WorkActor
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.media import directory_index
from shadow_mdc.services.catalog_people import (
    MergeRequest,
    PersonDocument,
    PersonEdit,
    edit_person,
    import_people,
    merge_people,
    person,
    undo_merge,
)
from shadow_mdc.services.gfriends import GfriendsActorImageResolver
from shadow_mdc.services.gfriends_fill import GfriendsFillStats


def test_source_placeholders_do_not_become_actor_identities() -> None:
    record = ProviderRecord(
        provider="fixture", external_id="movie", title="Movie",
        actors=("Named actor", "(YOKO)", "他", "\uff08その他\uff09", "unknown", "Uncredited", "---", " "),
    )
    assert record.actors == ("Named actor", "(YOKO)")


def test_merge_preserves_edited_source_details_and_undo(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'people.db'}")
    database.initialize()
    with database.session() as session:
        session.add_all([Work(id="a", title="A"), Work(id="b", title="B")])
        session.flush()
        import_people(session, [
            PersonDocument(id="one", ref="jav:one", name="Original", aliases=["Old alias"], work_ids=["a"]),
            PersonDocument(id="two", ref="jav:two", name="Source", work_ids=["b"]),
        ])
        edit_person(session, "one", PersonEdit(
            name="Preferred", aliases=["Manual alias"], notes="Target note", groups=["Target"], revision=1,
        ))
        edit_person(session, "two", PersonEdit(
            name="Source edited", aliases=["Source alias"], biography="Source biography",
            image_url="https://example.com/portrait.jpg", notes="Source note",
            match_names=["Hint"], revision=1,
        ))
        before = person(session, "one", private=True)
        merged = merge_people(session, MergeRequest(
            target_id="one", source_id="two", target_revision=2, source_revision=2,
        ))
        current = person(session, "two", private=True)
        assert current.id == "one" and current.name == "Preferred"
        assert set(current.aliases) >= {"Original", "Manual alias", "Source", "Source edited", "Source alias"}
        assert current.biography == "Source biography"
        assert current.image_url == "https://example.com/portrait.jpg"
        assert current.work_ids == ["a", "b"] and current.match_names == ["Hint"]
        assert current.notes == "Target note\n\nSource note"
        undo_merge(session, merged.id)
        restored = person(session, "one", private=True)
        assert restored.model_dump(exclude={"revision"}) == before.model_dump(exclude={"revision"})


def test_partial_and_unchanged_edits_allow_later_source_image(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'people.db'}")
    database.initialize()
    with database.session() as session:
        original = PersonDocument(id="one", ref="jav:one", name="Original", biography="Keep")
        import_people(session, [original])
        edit_person(session, "one", PersonEdit(name="Preferred", image_url=None, revision=1))
        import_people(session, [original.model_copy(update={"image_url": "https://example.com/new.jpg"})])
        updated = person(session, "one", private=True)
        assert updated.biography == "Keep" and updated.image_url == "https://example.com/new.jpg"
        edit_person(session, "one", PersonEdit(name="Preferred", image_url=None, revision=updated.revision))
        assert person(session, "one", private=True).image_url is None


def test_skill_merge_partial_update_and_image_echo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'skill.db'}")
    monkeypatch.setenv("SHADOW_MDC_MCP_TOKEN", "REPLACE_TEST")
    with TestClient(app) as client:
        first = client.post("/api/catalog/people", json={
            "name": "One", "biography": "Keep", "notes": "Keep notes", "revision": 1,
            "image_url": "https://example.com/a.jpg",
        }).json()
        second = client.post("/api/catalog/people", json={"name": "Two", "revision": 1}).json()
        first = client.get("/api/catalog/people/" + first["id"]).json()
        client.headers.update({"Authorization": "Bearer REPLACE_TEST", "X-Catalog-Owner": "test-owner"})
        response = client.post("/api/skill/v1/call", json={"name": "actors.update", "arguments": {
            "person_id": first["id"], "name": "Renamed", "revision": 1, "image_url": first["image_url"],
        }})
        assert response.json()["ok"], response.text
        assert response.json()["result"]["biography"] == "Keep"
        assert response.json()["result"]["notes"] == "Keep notes"
        with app.state.runtime.database.session() as session:
            assert person(session, first["id"], private=True).image_url == "https://example.com/a.jpg"
        merged = client.post("/api/skill/v1/call", json={"name": "actors.merge", "arguments": {
            "target_id": first["id"], "source_id": second["id"], "target_revision": 2, "source_revision": 1,
        }}).json()
        assert merged["ok"]
        history = client.post("/api/skill/v1/call", json={"name": "actors.merges"}).json()
        assert history["ok"] and len(history["result"]) == 1
        undo = client.post("/api/skill/v1/call", json={"name": "actors.merge.undo", "arguments": {
            "merge_id": merged["result"]["merge_id"],
        }}).json()
        assert undo["ok"]


def test_credit_case_fallback_uses_unique_identity(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'credits.db'}")
    with TestClient(app):
        runtime = app.state.runtime
        with runtime.database.session() as session:
            work = Work(id="work", title="A", actors=["RARA"])
            actor = Actor(id="one", name="Rara", normalized_name="rara")
            session.add_all([work, actor])
            session.flush()
            session.add(WorkActor(work_id=work.id, actor_id=actor.id))
            session.add(CatalogPersonCredit(person_id=actor.id, work_id=work.id, role="actor"))
            session.flush()
            assert catalog_credit_names(runtime, session, [work])[work.id] == ["Rara"]
            import_people(session, [
                PersonDocument(id="two", ref="person:two", name="Rara", work_ids=[work.id]),
            ])
            # A second linked person with the same name remains a distinct credit.
            assert catalog_credit_names(runtime, session, [work])[work.id].count("Rara") == 2


@pytest.mark.parametrize("cleared", [False, True])
def test_portrait_result_counts_visible_merged_profiles(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, cleared: bool,
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{tmp_path / 'portraits.db'}")

    def fill(repo: Repository, resolver: GfriendsActorImageResolver, **kwargs: object) -> GfriendsFillStats:
        directory = kwargs["actor_images_dir"]
        assert isinstance(directory, Path)
        directory.mkdir(parents=True, exist_ok=True)
        output = BytesIO()
        Image.new("RGB", (256, 256)).save(output, format="JPEG")
        for key in ("one", "two"):
            actor = repo._session.get(Actor, key)
            assert actor is not None
            (directory / f"{key}.jpg").write_bytes(output.getvalue())
            actor.image_url = f"/api/actor-images/{key}.jpg"
        directory_index.invalidate(directory)
        repo._session.flush()
        return GfriendsFillStats(2, 2, 2, 0, 0, 2, 0, False, "test", 2)

    monkeypatch.setattr(api_module, "fill_actor_images_from_gfriends", fill)
    with TestClient(app) as client:
        runtime = app.state.runtime
        with runtime.database.session() as session:
            session.add_all([
                Actor(id="one", name="Source A", normalized_name="source a"),
                Actor(id="two", name="Source B", normalized_name="source b"),
            ])
            session.flush()
            read_catalog_people(runtime, session, private=True)
            import_people(session, [PersonDocument(
                id="target", ref="person:target", name="Preferred",
                image_url="https://example.com/old.jpg" if cleared else None,
            )])
            if cleared:
                edit_person(session, "target", PersonEdit(name="Preferred", image_url=None, revision=1))
            for key in ("one", "two"):
                current = person(session, "target", private=True)
                merge_people(session, MergeRequest(
                    target_id="target", source_id=key, target_revision=current.revision, source_revision=1,
                ))
        response = client.post("/api/actors/fill-gfriends-images", json={})
        assert response.status_code == 200, response.text
        assert response.json()["filled"] == response.json()["downloaded"] == 2
        assert response.json()["visible_profiles"] == int(not cleared)
