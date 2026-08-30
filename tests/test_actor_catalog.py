from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.identity import IdentityAliasRules, build_identity_hints
from shadow_mdc.services.actor_catalog import (
    ActorCatalogStore,
    ActorProfile,
    ActorWorkReference,
    build_actor_catalog,
    enrich_actor_aliases,
    filter_jav_actor_catalog,
    sync_actor_catalog,
)


def test_actor_catalog_groups_aliases_and_enriches_future_path_matching(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 'actors.db'}")
    database.initialize()
    rules = IdentityAliasRules(actors={"乃蒼": "羽月乃蒼"})
    with database.session() as session:
        repo = Repository(session)
        repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="one",
                code="CAWD-797",
                title="中文标题",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                actors=("乃蒼",),
            ),
            overwrite=True,
        )
        catalog = build_actor_catalog(repo.list_works(), rules)

    assert len(catalog) == 1
    assert catalog[0].name == "羽月乃蒼"
    assert catalog[0].aliases == ("乃蒼",)
    assert catalog[0].work_count == 1

    enriched = enrich_actor_aliases(rules, catalog)
    hints = build_identity_hints(
        "V (1).strm",
        context_names=("羽月乃蒼", "Japan"),
        alias_rules=enriched,
    )
    assert hints.actors == ("羽月乃蒼",)


def test_actor_catalog_survives_media_database_reset(tmp_path: Path) -> None:
    store = ActorCatalogStore(tmp_path / "actor-catalog.json")
    rules = IdentityAliasRules()
    first_database = Database(f"sqlite:///{tmp_path / 'first.db'}")
    first_database.initialize()
    with first_database.session() as session:
        first_repo = Repository(session)
        first_repo.upsert_provider_record(
            ProviderRecord(
                provider="fixture",
                external_id="old-work",
                code="CAWD-797",
                title="旧作品",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                actors=("演员甲",),
            ),
            overwrite=True,
        )
        assert sync_actor_catalog(store, first_repo.list_works(), rules)[0].work_count == 1

    reset_database = Database(f"sqlite:///{tmp_path / 'reset.db'}")
    reset_database.initialize()
    with reset_database.session() as session:
        reset_repo = Repository(session)
        preserved = sync_actor_catalog(store, reset_repo.list_works(), rules)

    assert len(preserved) == 1
    assert preserved[0].name == "演员甲"
    assert preserved[0].works[0].code == "CAWD-797"


def test_actor_catalog_removes_non_jav_and_code_less_legacy_works() -> None:
    profiles = (
        ActorProfile(
            name="混合演员",
            aliases=(),
            categories=("China", "Europe", "Japan"),
            work_count=4,
            works=(
                ActorWorkReference(id="jav", title="JAV", code="SONE-118", category="Japan"),
                ActorWorkReference(id="local", title="旧误判", code=None, category="Japan"),
                ActorWorkReference(id="china", title="国产", code="MDSR-001", category="China"),
                ActorWorkReference(id="west", title="欧美", code=None, category="Europe"),
            ),
        ),
        ActorProfile(
            name="非JAV演员",
            aliases=(),
            categories=("Europe",),
            work_count=1,
            works=(ActorWorkReference(id="only-west", title="Scene", code=None, category="Europe"),),
        ),
        ActorProfile(
            name="7sht.me",
            aliases=(),
            categories=("Japan",),
            work_count=1,
            works=(
                ActorWorkReference(
                    id="domain-noise",
                    title="FC2",
                    code="FC2-1022799",
                    category="Japan",
                ),
            ),
        ),
        ActorProfile(
            name="未知",
            aliases=(),
            categories=("Japan",),
            work_count=1,
            works=(
                ActorWorkReference(
                    id="unknown-noise",
                    title="JAV",
                    code="DASS-307",
                    category="Japan",
                ),
            ),
        ),
    )

    filtered = filter_jav_actor_catalog(profiles)

    assert len(filtered) == 1
    assert filtered[0].name == "混合演员"
    assert filtered[0].categories == ("Japan",)
    assert [(work.code, work.category) for work in filtered[0].works] == [("SONE-118", "Japan")]
