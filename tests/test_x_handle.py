from pathlib import Path

from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services.non_jav_actor_catalog import (
    build_non_jav_actor_profile,
    normalize_x_handle,
    x_profile_url,
)


def test_normalize_x_handle_variants() -> None:
    assert normalize_x_handle("@Hello_World") == "Hello_World"
    assert normalize_x_handle("https://x.com/Hello_World") == "Hello_World"
    assert normalize_x_handle("https://twitter.com/Hello_World?s=20") == "Hello_World"
    assert normalize_x_handle("bad handle") is None
    assert x_profile_url("@abc") == "https://x.com/abc"


def test_non_jav_actor_persists_x_handle(tmp_path: Path, monkeypatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")
    with TestClient(app) as client:
        created = client.post(
            "/api/non-jav-actors",
            json={
                "name": "Test Creator",
                "aliases": [],
                "groups": ["blogger"],
                "categories": ["China"],
                "x_handle": "@DemoUser",
                "biography": None,
                "notes": None,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["x_handle"] == "DemoUser"
        assert body["x_url"] == "https://x.com/DemoUser"
        listed = client.get("/api/non-jav-actors").json()
        assert any(item["name"] == "Test Creator" and item["x_handle"] == "DemoUser" for item in listed)


def test_actor_merge_keeps_x_handle(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'merge.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        keep = repo._session  # noqa: SLF001
        from shadow_mdc.db.models import Actor

        a = Actor(name="Keep", normalized_name="keep", x_handle=None)
        b = Actor(name="Drop", normalized_name="drop", x_handle="from_drop")
        session.add_all([a, b])
        session.flush()
        merged = repo.merge_actors(keep_actor_id=a.id, drop_actor_id=b.id)
        assert merged.x_handle == "from_drop"


def test_build_profile_normalizes_handle() -> None:
    profile = build_non_jav_actor_profile(
        name="Creator",
        aliases=(),
        groups=("independent",),
        categories=(),
        x_handle="https://x.com/FooBar",
    )
    assert profile.x_handle == "FooBar"
