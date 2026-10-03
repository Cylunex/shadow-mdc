"""JavDB detail parsing against a trimmed real page (navbar + info panel).

Regression: the old page-wide ``a[href*="/actors/"]`` / ``a[href*="/tags/"]`` scrape picked
up the navbar dropdowns (有碼/無碼/歐美/FC2/動漫), took the code ``<strong>`` as the title,
and read ``番號:`` as the code so works were seeded with ``primary_code = NULL``.
"""

from pathlib import Path

import httpx
import pytest

from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import ContentFamily, QueryMode
from shadow_mdc.providers.javdb import JavDBProvider, parse_javdb_detail

FIXTURES = Path(__file__).parent / "fixtures"
NAV_LABELS = {"有碼", "無碼", "歐美", "FC2", "動漫", "類別", "推薦"}


def _fixture() -> str:
    return (FIXTURES / "javdb_detail_dldss546.html").read_text(encoding="utf-8")


def test_detail_panel_fields_ignore_site_navbar() -> None:
    record = parse_javdb_detail(_fixture(), "https://javdb.com/v/AqJ5AK")

    assert record.code == "DLDSS-546"
    assert record.external_id == "AqJ5AK"
    assert record.title == "市民プールそのうち出禁確定。 ’バグった’競泳水着の人妻が週3泳ぎにやってきますー。 叶愛"
    assert record.title != record.code
    # Female performers only; the four male actors are unmarked in the panel.
    assert record.actors == ("叶愛",)
    assert record.tags == ("熟女", "已婚婦女", "巨乳", "學校泳裝", "單體作品", "中出")
    assert not NAV_LABELS & set(record.actors)
    assert not NAV_LABELS & set(record.tags)
    assert record.studio == "DAHLIA"
    assert record.directors == ("ひむろっく",)
    assert str(record.release_date) == "2026-10-08"
    assert record.runtime_seconds == 145 * 60
    assert record.rating == pytest.approx(4.31)
    assert record.rating_count == 580
    # Generic site description is not a plot.
    assert record.plot is None
    assert str(record.artwork[0].url) == "https://c0.jdbstatic.com/covers/aq/AqJ5AK.jpg"


def test_older_layout_gender_symbols() -> None:
    html = """
    <h2 class="title is-4"><strong>ABP-001 </strong><strong class="current-title">Real title</strong></h2>
    <nav class="panel movie-panel-info">
      <div class="panel-block first-block"><strong>番號:</strong>
        <span class="value"><a href="/video_codes/ABP">ABP</a>-001</span></div>
      <div class="panel-block"><strong>演員:</strong><span class="value">
        <a href="/actors/a1">Alice</a><strong class="symbol female">♀</strong>&nbsp;
        <a href="/actors/m1">Bob</a><strong class="symbol male">♂</strong>&nbsp;
      </span></div>
    </nav>
    """
    record = parse_javdb_detail(html, "https://javdb.com/v/x1")
    assert record.code == "ABP-001"
    assert record.title == "Real title"
    assert record.actors == ("Alice",)


def test_minimal_page_without_panel_skips_navbar_links() -> None:
    html = """
    <nav class="navbar"><div class="navbar-dropdown">
      <a class="navbar-item" href="/actors/censored">有碼</a>
      <a class="navbar-item" href="/tags/uncensored?c10=1">無碼</a>
      <a class="navbar-item" href="/makers/uncensored">無碼</a>
    </div></nav>
    <h2 class="title">SSIS-123 Fixture title</h2>
    <a href="/makers/studio">Fixture Studio</a>
    <a href="/actors/alice">Alice</a>
    <a href="/tags/drama">Drama</a>
    """
    record = parse_javdb_detail(html, "https://javdb.com/v/y1")
    assert record.actors == ("Alice",)
    assert record.tags == ("Drama",)
    assert record.studio == "Fixture Studio"


@pytest.mark.asyncio
async def test_provider_url_lookup_uses_panel_parser() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture(), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        provider = JavDBProvider(client, "https://javdb.com")
        records = await provider.search(
            IdentityHints(
                term="https://javdb.com/v/AqJ5AK",
                mode=QueryMode.URL,
                family=ContentFamily.UNKNOWN,
                source_url="https://javdb.com/v/AqJ5AK",
            )
        )

    assert len(records) == 1
    assert records[0].code == "DLDSS-546"
    assert records[0].actors == ("叶愛",)


def test_hidden_origin_title_preferred_over_zh_translation() -> None:
    html = """
    <h2 class="title is-4">
      <strong>JMD-133 </strong>
      <strong class="current-title">祭品叔母 ～能讓我無套跟你家美女媽來一發嘛～ 佐佐木明希 </strong>
      <a href="javascript:;" class="meta-link" data-movie-detail-target="showOriginTitle">顯示原標題</a>
      <span style="display: none" class="origin-title">いけにえ叔母さん～お前んちの美人な母ちゃん、生でヤラしてくれないかな～ 佐々木あき</span>
    </h2>
    <nav class="panel movie-panel-info">
      <div class="panel-block first-block"><strong>番號:</strong>
        <span class="value"><a href="/video_codes/JMD">JMD</a>-133</span>
        <a class="button is-white copy-to-clipboard" data-clipboard-text="JMD-133"></a></div>
      <div class="panel-block"><strong>演員:</strong><span class="value">
        <a class="actor-female" href="/actors/ZOM6">佐々木あき</a>, <a href="/actors/vDWnz">???</a>,
        <a href="/actors/MmXQA">柏木純吉</a></span></div>
    </nav>
    """
    record = parse_javdb_detail(html, "https://javdb.com/v/ZNG0X")
    assert record.code == "JMD-133"
    assert record.title == "いけにえ叔母さん～お前んちの美人な母ちゃん、生でヤラしてくれないかな～ 佐々木あき"
    assert record.original_title == record.title
    assert record.actors == ("佐々木あき",)
