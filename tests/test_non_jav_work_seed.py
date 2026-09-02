from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.enums import MediaCategory
from shadow_mdc.services.non_jav_actor_catalog import NonJavActorCatalogStore, build_non_jav_actor_profile
from shadow_mdc.services.non_jav_work_seed import seed_non_jav_works


def test_seed_non_jav_works_upserts_and_links_actors(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "actor-images").mkdir()
    (data_dir / "artwork").mkdir()
    seed_path = data_dir / "non-jav-works.json"
    seed_path.write_text(
        """
{
  "version": 1,
  "source": "fixture",
  "works": [
    {
      "id": "fixture-angela-1",
      "title": "Angela Fixture Feature",
      "family": "western",
      "category": "Europe",
      "year": 2018,
      "studio": "Fixture Studio",
      "series": "Fixture",
      "actors": ["Angela White"],
      "tags": ["western"],
      "ensure_actors": [
        {
          "name": "Mia Seed",
          "aliases": ["Mia S"],
          "groups": ["western"],
          "categories": ["Europe"],
          "biography": "Seeded"
        }
      ]
    },
    {
      "id": "fixture-mia-1",
      "title": "Mia Seed Feature",
      "family": "western",
      "category": "Europe",
      "year": 2019,
      "studio": "Fixture Studio",
      "actors": ["Mia Seed"],
      "tags": ["western"]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    actor_store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    actor_store.upsert(
        build_non_jav_actor_profile(
            name="Angela White",
            aliases=("Angela",),
            groups=("western",),
            categories=(MediaCategory.EUROPE,),
        )
    )
    database = Database(f"sqlite:///{data_dir / 'seed.db'}")
    database.initialize()
    with database.session() as session:
        first = seed_non_jav_works(
            Repository(session),
            seed_path=seed_path,
            actor_store=actor_store,
            actor_images_dir=data_dir / "actor-images",
            artwork_dir=data_dir / "artwork",
        )
        second = seed_non_jav_works(
            Repository(session),
            seed_path=seed_path,
            actor_store=actor_store,
            actor_images_dir=data_dir / "actor-images",
            artwork_dir=data_dir / "artwork",
        )
        works = Repository(session).list_works()

    assert first.created == 2
    assert first.actors_added == 1
    assert first.posters == 2
    assert second.created == 0
    assert second.updated == 2
    assert {work.title for work in works} == {"Angela Fixture Feature", "Mia Seed Feature"}
    assert actor_store.get("Mia Seed") is not None
    assert actor_store.get("Mia Seed").image_file is None


def test_non_jav_actors_api_exposes_seeded_works(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'api-seed.db'}")
    (data_dir / "non-jav-works.json").write_text(
        """
{
  "version": 1,
  "works": [
    {
      "id": "api-seed-1",
      "title": "Creator Seed Work",
      "family": "western",
      "category": "Europe",
      "year": 2020,
      "studio": "Seed",
      "actors": ["Seed Star"],
      "ensure_actors": [
        {"name": "Seed Star", "groups": ["western"], "categories": ["Europe"]}
      ]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )

    with TestClient(app) as client:
        actors = client.get("/api/non-jav-actors").json()
        works = client.get("/api/works").json()

    assert len(works) == 1
    assert works[0]["title"] == "Creator Seed Work"
    assert works[0]["image_url"]
    matched = next(actor for actor in actors if actor["name"] == "Seed Star")
    assert matched["work_count"] == 1
    assert matched["works"][0]["title"] == "Creator Seed Work"
