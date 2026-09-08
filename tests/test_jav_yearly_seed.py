from pathlib import Path

from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.jav_yearly_seed import (
    JavYearlySeedCatalog,
    dmm_content_id,
    guess_dmm_cover_urls,
    seed_jav_yearly_top,
)


def test_dmm_content_id_padding() -> None:
    assert dmm_content_id("SSIS-129") == "ssis00129"
    assert dmm_content_id("SNIS-786") == "snis00786"
    assert guess_dmm_cover_urls("SSIS-129")[0].endswith("/ssis00129pl.jpg")


def test_seed_jav_yearly_top_idempotent(tmp_path: Path) -> None:
    data_dir = tmp_path / "data"
    artwork = data_dir / "artwork"
    artwork.mkdir(parents=True)
    seed_path = data_dir / "jav-yearly-top.json"
    seed_path.write_text(
        """
{
  "version": 1,
  "source": "fixture",
  "years": [
    {
      "year": 2018,
      "actresses": [
        {
          "rank": 1,
          "name": "河北彩花",
          "aliases": ["Saika Kawakita"],
          "works": [
            {
              "code": "SSNI-190",
              "title": "新人NO.1STYLE 河北彩花AVデビュー",
              "year": 2018,
              "studio": "S1 NO.1 STYLE",
              "cover_url": null
            }
          ]
        }
      ]
    },
    {
      "year": 2021,
      "actresses": [
        {
          "rank": 1,
          "name": "河北彩花",
          "aliases": ["Saika Kawakita"],
          "works": [
            {
              "code": "SSIS-129",
              "title": "河北彩花 Re:start!",
              "year": 2021,
              "studio": "S1 NO.1 STYLE",
              "cover_url": null
            }
          ]
        }
      ]
    }
  ]
}
""".strip(),
        encoding="utf-8",
    )
    catalog = JavYearlySeedCatalog.model_validate_json(seed_path.read_text(encoding="utf-8"))
    assert [bucket.year for bucket in catalog.years] == [2018, 2021]

    database = Database(f"sqlite:///{data_dir / 'seed.db'}")
    database.initialize()
    with database.session() as session:
        first = seed_jav_yearly_top(
            Repository(session),
            seed_path=seed_path,
            artwork_dir=artwork,
            download_posters=False,
        )
        second = seed_jav_yearly_top(
            Repository(session),
            seed_path=seed_path,
            artwork_dir=artwork,
            download_posters=False,
        )
        works = Repository(session).list_works()
        actors = {name for work in works for name in work.actors}

    assert first.created == 2
    assert first.works == 2
    assert first.actresses == 1
    assert first.years == (2018, 2021)
    assert second.created == 0
    assert second.updated == 2
    assert {work.primary_code for work in works} == {"SSNI-190", "SSIS-129"}
    assert all(work.family == ContentFamily.JAV.value for work in works)
    assert all(work.category == MediaCategory.JAPAN.value for work in works)
    assert actors == {"河北彩花"}
