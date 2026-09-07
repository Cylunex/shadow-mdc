from __future__ import annotations

import json
from pathlib import Path, PurePosixPath

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.catalog_export import (
    compute_export_state,
    export_catalog_bundle,
    load_export_state,
    state_path,
)
from shadow_mdc.services.non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    build_non_jav_actor_profile,
)


def _seed_catalog(data_dir: Path) -> Path:
    data_dir.mkdir(parents=True, exist_ok=True)
    (data_dir / "actor-images").mkdir()
    (data_dir / "artwork").mkdir()
    database_path = data_dir / "shadow-mdc.db"
    database = Database(f"sqlite:///{database_path}")
    database.initialize()

    actor_store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    actor_store.upsert(
        build_non_jav_actor_profile(
            name="麻豆演员甲",
            aliases=("演员甲",),
            groups=("madou",),
            categories=(MediaCategory.CHINA,),
            image_file="actor-a.jpg",
        )
    )
    (data_dir / "actor-images" / "actor-a.jpg").write_bytes(b"avatar-a")

    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="md-001",
                title="首部作品",
                family=ContentFamily.CHINESE,
                category=MediaCategory.CHINA,
                studio="麻豆传媒",
                series="MDX",
                actors=("麻豆演员甲",),
            ),
            overwrite=True,
        )
        art = data_dir / "artwork" / work.id
        art.mkdir(parents=True)
        poster = art / "poster.jpg"
        poster.write_bytes(b"poster-1")
        repo.update_artwork_local_paths(work, {"https://example/poster.jpg": str(poster)})
        work_id = work.id

    (data_dir / "non-jav-works.json").write_text(
        json.dumps(
            {
                "version": 1,
                "source": "test",
                "works": [
                    {
                        "id": work_id,
                        "title": "首部作品",
                        "actors": ["麻豆演员甲"],
                        "studio": "麻豆传媒",
                        "series": "MDX",
                    }
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return database_path


def test_full_then_incremental_exports_only_changes(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    database_path = _seed_catalog(data_dir)
    target = PurePosixPath("/data")

    full_out = tmp_path / "full-bundle"
    full = export_catalog_bundle(
        source_data_dir=data_dir,
        source_database=database_path,
        output=full_out,
        target_data_dir=target,
    )
    assert full.mode == "full"
    assert (full_out / "data" / "shadow-mdc.db").is_file()
    assert (full_out / "data" / "actor-images" / "actor-a.jpg").is_file()
    assert full.catalog_counts.get("works", 0) >= 1
    assert state_path(data_dir).is_file()

    # Second incremental with no changes should pack empty/near-empty deltas.
    empty_out = tmp_path / "empty-incremental"
    empty = export_catalog_bundle(
        source_data_dir=data_dir,
        source_database=database_path,
        output=empty_out,
        target_data_dir=target,
        incremental=True,
    )
    assert empty.mode == "incremental"
    assert empty.incremental["works"] == []
    assert empty.incremental["actors"] == []
    assert empty.incremental["actor_images"] == []
    assert empty.catalog_counts.get("works", 0) == 0

    # Mutate catalog: new actor image + new work.
    (data_dir / "actor-images" / "actor-b.jpg").write_bytes(b"avatar-b")
    actor_store = NonJavActorCatalogStore(data_dir / "non-jav-actors.json")
    actor_store.upsert(
        build_non_jav_actor_profile(
            name="探花演员乙",
            aliases=("演员乙",),
            groups=("tanhua",),
            categories=(MediaCategory.CHINA,),
            image_file="actor-b.jpg",
        )
    )
    database = Database(f"sqlite:///{database_path}")
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="th-002",
                title="新增作品",
                family=ContentFamily.CHINESE,
                category=MediaCategory.CHINA,
                studio="探花",
                actors=("探花演员乙",),
            ),
            overwrite=True,
        )
        art = data_dir / "artwork" / work.id / "poster.jpg"
        art.parent.mkdir(parents=True)
        art.write_bytes(b"poster-2")
        repo.update_artwork_local_paths(work, {"https://example/p2.jpg": str(art)})
        new_work_id = work.id

    delta_out = tmp_path / "delta-bundle"
    delta = export_catalog_bundle(
        source_data_dir=data_dir,
        source_database=database_path,
        output=delta_out,
        target_data_dir=target,
        incremental=True,
    )
    assert delta.mode == "incremental"
    assert new_work_id in delta.incremental["works"]
    assert "actor-b.jpg" in delta.incremental["actor_images"]
    assert (delta_out / "data" / "actor-images" / "actor-b.jpg").is_file()
    assert not (delta_out / "data" / "actor-images" / "actor-a.jpg").exists()
    assert delta.catalog_counts.get("works", 0) == 1

    # Fingerprint state advanced.
    state = load_export_state(state_path(data_dir))
    current = compute_export_state(data_dir=data_dir, database=database_path)
    assert state.works == current.works
    assert state.actor_images == current.actor_images
