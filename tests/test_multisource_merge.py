from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

import pytest
from pydantic import HttpUrl

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory, QueryMode
from shadow_mdc.providers.base import ProviderRegistry
from shadow_mdc.services.identify import IdentifyService


@dataclass(frozen=True)
class RecordProvider:
    record: ProviderRecord

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=self.record.provider,
            name=self.record.provider,
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        return [self.record]


@pytest.fixture
def repository(tmp_path: Path) -> Iterator[Repository]:
    database = Database(f"sqlite:///{tmp_path / 'merge.db'}")
    database.initialize()
    with database.session() as session:
        yield Repository(session)


@pytest.mark.asyncio
async def test_exact_code_sources_merge_fields_and_keep_provenance(
    repository: Repository,
    tmp_path: Path,
) -> None:
    root = tmp_path / "media"
    root.mkdir()
    source = root / "SONE-118.mp4"
    source.write_bytes(b"fixture")
    library = repository.create_library(
        name="JAV",
        root_path=str(root),
        category=MediaCategory.JAPAN,
        recursive=True,
        organize_template="{code_or_title}.{ext}",
    )
    hints = IdentityHints(
        term="SONE-118",
        mode=QueryMode.CODE,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        code="SONE-118",
        file_path=str(source),
    )
    asset, _ = repository.upsert_asset(
        library_id=library.id,
        path=str(source),
        size=source.stat().st_size,
        duration_seconds=None,
        oshash=None,
        hints=hints,
    )
    primary = ProviderRecord(
        provider="a-primary",
        external_id="one",
        code="SONE-118",
        title="Primary title",
        family=ContentFamily.JAV,
        studio="Studio",
        actors=("Alice",),
    )
    supplemental = ProviderRecord(
        provider="b-supplemental",
        external_id="two",
        code="sone-118",
        title="Secondary title",
        family=ContentFamily.JAV,
        plot="A complete plot",
        actors=("Bob",),
        tags=("Drama",),
        artwork=(Artwork(url=HttpUrl("https://img.example/poster.jpg"), kind="poster"),),
    )
    result = await IdentifyService(
        repository,
        ProviderRegistry([RecordProvider(primary), RecordProvider(supplemental)]),
    ).identify(asset.id)

    assert result.accepted_work_id is not None
    work = repository.get_work(result.accepted_work_id)
    assert work is not None
    assert work.title == "Primary title"
    assert work.plot == "A complete plot"
    assert work.actors == ["Alice", "Bob"]
    assert work.tags == ["Drama"]
    assert work.field_sources["title"] == "a-primary"
    assert work.field_sources["plot"] == "b-supplemental"
    assert {item.provider for item in repository.identities_for_work(work.id)} >= {
        "a-primary",
        "b-supplemental",
    }


def test_repair_jav_actors_prefers_structured_provider_snapshot(repository: Repository) -> None:
    noisy = ProviderRecord(
        provider="freejavbt",
        external_id="mida-008",
        code="MIDA-008",
        title="Title",
        family=ContentFamily.JAV,
        actors=("Director-like credit", "Reina Miyashita"),
    )
    stored = repository._create_work(noisy)
    repository._upsert_snapshot(stored, noisy)
    reliable = noisy.model_copy(
        update={
            "provider": "jav321",
            "external_id": "mida00008",
            "actors": ("Reina Miyashita",),
        }
    )
    repository._upsert_snapshot(stored, reliable)

    assert repository.repair_jav_actor_sources() == 1
    assert stored.actors == ["Reina Miyashita"]
    assert stored.field_sources["actors"] == "jav321"
