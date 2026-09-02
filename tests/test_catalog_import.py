from __future__ import annotations

import json
from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.enums import MediaCategory, RecognitionScope
from shadow_mdc.services.catalog_import import (
    CatalogImportRequest,
    import_catalog_bundle,
    merge_non_jav_actor_profiles,
)
from shadow_mdc.services.non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    build_non_jav_actor_profile,
)

FIXTURE_BUNDLE = Path(__file__).parent / "fixtures" / "catalog-import-bundle"


def test_merge_non_jav_actor_profiles_keeps_existing_and_enriches() -> None:
    existing = build_non_jav_actor_profile(
        name="Existing Star",
        aliases=("Existing Alias",),
        groups=("western",),
        categories=(MediaCategory.EUROPE,),
        biography="Local bio",
        notes="keep me",
    )
    incoming = build_non_jav_actor_profile(
        name="Existing Star",
        aliases=("Richer Alias",),
        groups=("feature",),
        categories=(MediaCategory.EUROPE,),
        image_file="incoming-avatar.png",
        biography="Incoming longer biography for existing actor.",
    )
    merged = merge_non_jav_actor_profiles(existing, incoming)
    assert merged.name == "Existing Star"
    assert "Existing Alias" in merged.aliases
    assert "Richer Alias" in merged.aliases
    assert set(merged.groups) == {"western", "feature"}
    assert merged.image_file == "incoming-avatar.png"
    assert merged.biography == "Local bio"
    assert merged.notes == "keep me"


def test_import_catalog_bundle_merges_actors_and_seeds_works_without_wiping_libraries(
    tmp_path: Path,
) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "actor-images").mkdir()
    (data_dir / "artwork").mkdir()

    actor_store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    actor_store.upsert(
        build_non_jav_actor_profile(
            name="Existing Star",
            aliases=("Existing Alias",),
            groups=("western",),
            categories=(MediaCategory.EUROPE,),
            biography="Local bio",
            notes="keep me",
        )
    )

    database = Database(f"sqlite:///{data_dir / 'shadow-mdc.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        library = repo.create_library(
            name="Keep Me",
            root_path=str(tmp_path / "media"),
            category=MediaCategory.OTHER,
            recursive=True,
            organize_template="{actor}/{folder_name}/{media_name}.{ext}",
            recognition_scope=RecognitionScope.ALL,
        )
        library_id = library.id
        before_libraries = len(repo.list_libraries())

        result = import_catalog_bundle(
            bundle=FIXTURE_BUNDLE,
            data_dir=data_dir,
            repo=repo,
            actor_store=actor_store,
            actor_images_dir=data_dir / "actor-images",
            artwork_dir=data_dir / "artwork",
            request=CatalogImportRequest(include_formal=False),
        )

        after_libraries = repo.list_libraries()
        works = repo.list_works()

    assert before_libraries == 1
    assert len(after_libraries) == 1
    assert after_libraries[0].id == library_id
    assert result.actors_added == 1
    assert result.actors_updated == 1
    assert result.actor_images_copied >= 1
    assert result.works_created == 1
    assert {work.title for work in works} == {"Imported Feature"}

    existing = actor_store.get("Existing Star")
    assert existing is not None
    assert existing.biography == "Local bio"
    assert existing.notes == "keep me"
    assert "Richer Alias" in existing.aliases
    assert "feature" in existing.groups
    assert existing.image_file == "incoming-avatar.png"
    assert (data_dir / "actor-images" / "incoming-avatar.png").is_file()

    new_actor = actor_store.get("Brand New Star")
    assert new_actor is not None
    assert (data_dir / "artwork" / "fixture-import-1" / "poster.png").is_file()


def test_import_catalog_bundle_dry_run_does_not_write(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    (data_dir / "actor-images").mkdir()
    (data_dir / "artwork").mkdir()
    actor_store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    database = Database(f"sqlite:///{data_dir / 'shadow-mdc.db'}")
    database.initialize()
    with database.session() as session:
        result = import_catalog_bundle(
            bundle=FIXTURE_BUNDLE,
            data_dir=data_dir,
            repo=Repository(session),
            actor_store=actor_store,
            actor_images_dir=data_dir / "actor-images",
            artwork_dir=data_dir / "artwork",
            request=CatalogImportRequest(dry_run=True, include_formal=False),
        )
        works = Repository(session).list_works()

    assert result.dry_run is True
    assert result.actors_added == 2
    assert actor_store.load().actors == ()
    assert works == []
    assert not (data_dir / "actor-images" / "incoming-avatar.png").exists()


def test_import_local_package_layout_without_manifest(tmp_path: Path) -> None:
    package = tmp_path / "package"
    package.mkdir()
    (package / "actor-images").mkdir()
    (package / "artwork").mkdir()
    (package / "non-jav-actors.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "flat",
                "actors": [
                    {
                        "name": "Flat Actor",
                        "aliases": [],
                        "groups": ["independent"],
                        "categories": ["Other"],
                        "match_names": ["Flat Actor"],
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    data_dir = tmp_path / "dest"
    data_dir.mkdir()
    actor_store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    database = Database(f"sqlite:///{data_dir / 'db.sqlite'}")
    database.initialize()
    with database.session() as session:
        result = import_catalog_bundle(
            bundle=package,
            data_dir=data_dir,
            repo=Repository(session),
            actor_store=actor_store,
            actor_images_dir=data_dir / "actor-images",
            artwork_dir=data_dir / "artwork",
            request=CatalogImportRequest(actors_only=True, include_formal=False),
        )
    assert result.bundle_kind == "local-package"
    assert result.actors_added == 1
    assert actor_store.get("Flat Actor") is not None
