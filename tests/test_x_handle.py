from pathlib import Path
from unittest.mock import MagicMock

import httpx
import pytest
from fastapi.testclient import TestClient

import shadow_mdc.api as api_module
from shadow_mdc.api import app
from shadow_mdc.db.models import Actor
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.services import x_handle as x_mod
from shadow_mdc.services.non_jav_actor_catalog import (
    build_non_jav_actor_profile,
    parse_non_jav_actor_text,
)
from shadow_mdc.services.x_handle import (
    XHandleError,
    is_blocked_demo_x_handle,
    normalize_x_handle,
    require_verified_x_handle,
    sanitize_stored_x_handle,
    verify_x_handle_exists,
    x_profile_url,
)


def test_normalize_x_handle_variants() -> None:
    assert normalize_x_handle("@Hello_World") == "Hello_World"
    assert normalize_x_handle("https://x.com/Hello_World") == "Hello_World"
    assert normalize_x_handle("https://twitter.com/Hello_World?s=20") == "Hello_World"
    assert normalize_x_handle("bad handle") is None
    assert x_profile_url("@abc") == "https://x.com/abc"


@pytest.mark.parametrize(
    "handle",
    ["DemoUser", "DogfoodHandle", "testuser", "fake_user", "exampleUser", "dogfoodX"],
)
def test_blocked_demo_handles(handle: str) -> None:
    assert is_blocked_demo_x_handle(handle)
    assert sanitize_stored_x_handle(handle) is None
    with pytest.raises(XHandleError):
        require_verified_x_handle(f"@{handle}")


def test_require_verified_empty_clears() -> None:
    assert require_verified_x_handle(None) is None
    assert require_verified_x_handle("  ") is None


def test_verify_x_handle_exists_mocked_success() -> None:
    client = MagicMock(spec=httpx.Client)
    response = MagicMock()
    response.status_code = 200
    response.text = '<meta property="og:title" content="Real Person (@RealPerson99) on X">'
    client.get.return_value = response
    assert verify_x_handle_exists("RealPerson99", client=client) is True
    assert require_verified_x_handle("@RealPerson99", client=client) == "RealPerson99"


def test_verify_x_handle_exists_mocked_missing() -> None:
    client = MagicMock(spec=httpx.Client)
    response = MagicMock()
    response.status_code = 404
    response.text = '<meta property="og:title" content="User Profile Not Found - X | 404 Error">'
    client.get.return_value = response
    assert verify_x_handle_exists("NoSuchHandleZzz", client=client) is False
    with pytest.raises(XHandleError, match="could not be verified"):
        require_verified_x_handle("@NoSuchHandleZzz", client=client)


def test_parse_text_does_not_invent_x_handle_from_aliases() -> None:
    catalog = parse_non_jav_actor_text(
        """
### **四、OnlyFans热门女优**
Creator,Creator,@SomeAliasHandle
""",
        source="fixture.txt",
    )
    by_name = {actor.name: actor for actor in catalog.actors}
    assert "Creator" in by_name
    assert by_name["Creator"].x_handle is None
    assert any(alias.lstrip("@") == "SomeAliasHandle" for alias in by_name["Creator"].aliases)


def _patch_verify(monkeypatch: pytest.MonkeyPatch, fn) -> None:
    monkeypatch.setattr(x_mod, "verify_x_handle_exists", fn)
    monkeypatch.setattr(
        api_module,
        "require_verified_x_handle",
        lambda value, **kwargs: x_mod.require_verified_x_handle(value, verify=True),
    )


def _skip_heavy_seed(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid multi-minute non-JAV work seeding during focused X-handle API tests."""

    monkeypatch.setattr(
        "shadow_mdc.api.seed_non_jav_works",
        lambda *args, **kwargs: {"created": 0, "skipped": True},
    )


def test_non_jav_actor_rejects_demo_x_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _skip_heavy_seed(monkeypatch)
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
        assert created.status_code == 422, created.text


def test_non_jav_actor_persists_verified_x_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _skip_heavy_seed(monkeypatch)
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")

    def _fake_verify(handle: str, *, client=None, timeout: float = 12.0) -> bool:
        return normalize_x_handle(handle) == "VerifiedCreator"

    _patch_verify(monkeypatch, _fake_verify)

    with TestClient(app) as client:
        created = client.post(
            "/api/non-jav-actors",
            json={
                "name": "Test Creator",
                "aliases": [],
                "groups": ["blogger"],
                "categories": ["China"],
                "x_handle": "@VerifiedCreator",
                "biography": None,
                "notes": None,
            },
        )
        assert created.status_code == 201, created.text
        body = created.json()
        assert body["x_handle"] == "VerifiedCreator"
        assert body["x_url"] == "https://x.com/VerifiedCreator"
        listed = client.get("/api/non-jav-actors").json()
        assert any(
            item["name"] == "Test Creator" and item["x_handle"] == "VerifiedCreator" for item in listed
        )


def test_actor_patch_rejects_demo_handle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _skip_heavy_seed(monkeypatch)
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")

    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            row = Actor(name="PatchTarget", normalized_name="patchtarget", x_handle=None)
            session.add(row)
            session.flush()
            target_id = row.id
        rejected = client.patch(f"/api/actors/{target_id}", json={"x_handle": "@DemoUser"})
        assert rejected.status_code == 422, rejected.text


def test_actor_patch_accepts_mocked_verified(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    _skip_heavy_seed(monkeypatch)
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'test.db'}")

    def _fake_verify(handle: str, *, client=None, timeout: float = 12.0) -> bool:
        return normalize_x_handle(handle) == "OkHandle"

    _patch_verify(monkeypatch, _fake_verify)

    with TestClient(app) as client:
        with app.state.runtime.database.session() as session:
            row = Actor(name="PatchTarget2", normalized_name="patchtarget2", x_handle=None)
            session.add(row)
            session.flush()
            target_id = row.id
        ok = client.patch(f"/api/actors/{target_id}", json={"x_handle": "@OkHandle"})
        assert ok.status_code == 200, ok.text
        assert ok.json()["x_handle"] == "OkHandle"
        assert ok.json()["x_url"] == "https://x.com/OkHandle"


def test_actor_merge_keeps_x_handle(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'merge.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
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


def test_build_profile_strips_demo_handle() -> None:
    profile = build_non_jav_actor_profile(
        name="Creator",
        aliases=(),
        groups=("independent",),
        categories=(),
        x_handle="@DogfoodHandle",
    )
    assert profile.x_handle is None
