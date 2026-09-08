from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.jav_vr_yearly_seed import (
    JavVrYearlySeedCatalog,
    dmm_content_id,
    guess_vr_dmm_cover_urls,
    seed_jav_vr_yearly_top,
)


def test_vr_dmm_content_id_padding() -> None:
    assert dmm_content_id("SIVR-171") == "sivr00171"
    assert dmm_content_id("URVRSP-329") == "urvrsp00329"
    assert guess_vr_dmm_cover_urls("SIVR-171")[0].endswith("/sivr00171pl.jpg")


def test_seed_jav_vr_yearly_top_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    artwork = data_dir / "artwork"
    artwork.mkdir(parents=True)
    seed_path = data_dir / "jav-vr-yearly-top.json"
    seed_path.write_text(
        """
{
  "version": 1,
  "source": "fixture",
  "years": [
    {
      "year": 2021,
      "works": [
        {
          "rank": 1,
          "code": "SIVR-171",
          "title": "VR NO.1 STYLE 河北彩花 解禁",
          "year": 2021,
          "studio": "S1 NO.1 STYLE",
          "actors": ["河北彩花"],
          "tags": ["VR"],
          "cover_url": null
        }
      ]
    },
    {
      "year": 2024,
      "works": [
        {
          "rank": 1,
          "code": "URVRSP-329",
          "title": "パコサー飲み会VR",
          "year": 2024,
          "studio": "unfinished",
          "actors": ["百咲みいろ", "末広純"],
          "tags": ["VR"],
          "cover_url": null
        }
      ]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    catalog = JavVrYearlySeedCatalog.model_validate_json(seed_path.read_text(encoding="utf-8"))
    assert [bucket.year for bucket in catalog.years] == [2021, 2024]

    database = Database(f"sqlite:///{data_dir / 'seed.db'}")
    database.initialize()
    with database.session() as session:
        first = seed_jav_vr_yearly_top(
            Repository(session),
            seed_path=seed_path,
            artwork_dir=artwork,
            download_posters=False,
        )
        second = seed_jav_vr_yearly_top(
            Repository(session),
            seed_path=seed_path,
            artwork_dir=artwork,
            download_posters=False,
        )
        works = Repository(session).list_works()
        tags = {tag for work in works for tag in work.tags}

    assert first.created == 2
    assert first.works == 2
    assert first.years == (2021, 2024)
    assert second.created == 0
    assert second.updated == 2
    assert {work.primary_code for work in works} == {"SIVR-171", "URVRSP-329"}
    assert all(work.family == ContentFamily.JAV.value for work in works)
    assert all(work.category == MediaCategory.JAPAN.value for work in works)
    assert "VR" in tags
    assert "jav-vr-top-2021" in tags
    assert "jav-vr-top-2024-rank-1" in tags
