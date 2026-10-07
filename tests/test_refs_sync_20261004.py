"""Ports from the 2026-10-04 reference sync (javdb-cli, javinizer-go, JHS, p115client).

All offline: httpx.MockTransport + recorded fixtures.
"""

from __future__ import annotations

import asyncio
import gzip
import hashlib
import json
from collections.abc import Iterator
from pathlib import Path
from typing import Any

import httpx
import pytest

from shadow_mdc.actress_aliases import ActressNameMap, actor_identity_forms, merge_actor_names
from shadow_mdc.db.models import WorkMagnet
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.dmm_ids import content_id_candidates, known_content_id_prefixes
from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import QueryMode
from shadow_mdc.media.artwork import dmm_awsimgsrc_url
from shadow_mdc.media.magnets import MagnetLink, magnet_quality_flags, magnet_quality_score, rank_magnets
from shadow_mdc.providers.base import ProviderError, ProviderRegistry
from shadow_mdc.providers.challenge import challenge_kind, is_cloudflare_challenge, is_javbus_verify_page
from shadow_mdc.providers.javbus import JavBusProvider
from shadow_mdc.providers.javdb import JavDBProvider
from shadow_mdc.providers.javdb_api import (
    SIGNATURE_PREFIX,
    SIGNATURE_SUFFIX,
    JavDBApiAuthRequired,
    JavDBAppApi,
    female_actor_names,
    parse_magnets,
    parse_movie_detail,
    parse_movie_rows,
    sign,
)
from shadow_mdc.providers.mgstage import MgstageProvider, mgstage_product_candidates
from shadow_mdc.services.daily_chart_seed import collect_browse_pages
from shadow_mdc.services.discover import DiscoverService, javdb_movie_id_from_url, parse_javdb_list
from shadow_mdc.services.pan import FILE_GONE_CODES, PanApiError, _check_api_ok, api_error_code
from shadow_mdc.services.pan_offline_enqueue import pick_best_magnet
from shadow_mdc.services.r18_dump_download import (
    R18DumpDownloadError,
    download_latest_dump,
    parse_content_range,
)

FIXTURES = Path(__file__).parent / "fixtures"


def _json_fixture(name: str) -> dict[str, Any]:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _database(tmp_path: Path) -> Database:
    database = Database(f"sqlite:///{tmp_path / 'catalog.db'}")
    database.initialize()
    return database


# --- challenge detection (javinizer-go challengedetect) ---------------------------

CF_PAGE = """<!DOCTYPE html><html><head><title>Just a moment...</title></head>
<body><div id="challenge-running"></div><script src="/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1"></script>
</body></html>"""


def test_cloudflare_challenge_detected_and_normal_page_is_not() -> None:
    assert is_cloudflare_challenge(CF_PAGE)
    assert challenge_kind(CF_PAGE) is not None
    normal = (FIXTURES / "javbus_detail.html").read_text(encoding="utf-8")
    assert not is_cloudflare_challenge(normal)
    assert challenge_kind(normal) is None


def test_javbus_driver_verify_detected() -> None:
    assert is_javbus_verify_page("<html>age check</html>", "https://www.javbus.com/doc/driver-verify?referer=/ABP-123")


def test_provider_fails_fast_on_challenge_without_parsing() -> None:
    calls = 0

    async def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(403, text=CF_PAGE, headers={"content-type": "text/html"}, request=request)

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = JavBusProvider(client, "https://fixture.test", retries=3)
            with pytest.raises(ProviderError) as caught:
                await provider.search(IdentityHints(term="ABP-123", mode=QueryMode.CODE, code="ABP-123"))
            assert caught.value.reason == "blocked"

    asyncio.run(run())
    assert calls == 1  # a challenge is not retried


# --- JavDB app API (javdb-cli) ----------------------------------------------------


def test_sign_matches_javdb_cli_format() -> None:
    value = sign(1700000000)
    ts, suffix, digest = value.split(".")
    assert ts == "1700000000" and suffix == SIGNATURE_SUFFIX
    assert digest == hashlib.md5(f"1700000000{SIGNATURE_PREFIX}".encode()).hexdigest()


def test_parse_rankings_fixture() -> None:
    movies = parse_movie_rows(_json_fixture("javdb_api_rankings_daily.json")["data"], zone="censored")
    assert [movie.number for movie in movies] == ["IPZZ-937", "IPZZ-961", "MIDA-837"]
    assert movies[0].id == "Mb0JkJ" and movies[0].zone == "censored"


def test_parse_detail_keeps_only_female_performers_and_drops_nav_labels() -> None:
    record = parse_movie_detail(_json_fixture("javdb_api_movie_detail.json")["data"])
    assert record.code == "IPZZ-937"
    assert record.actors == ("桜空もも",)  # male 優生 and the injected 有碼 label are gone
    assert record.source_url == "https://javdb.com/v/Mb0JkJ"
    assert any(item.kind == "sample" for item in record.artwork)


def test_female_actor_names_without_gender_keeps_names_but_not_labels() -> None:
    assert female_actor_names([{"name": "無碼"}, {"name": "A"}, {"name": "歐美"}]) == ("A",)


def test_parse_magnets_fixture_sizes_in_bytes() -> None:
    magnets = parse_magnets(_json_fixture("javdb_api_magnets.json")["data"])
    assert len(magnets) == 3
    assert magnets[0].size_bytes == 4935 * 1024 * 1024
    assert all(len(item.info_hash) == 40 and item.uri.startswith("magnet:?xt=urn:btih:") for item in magnets)


def _api_handler(responses: dict[str, Any], seen: list[httpx.Request]):
    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        for path, payload in responses.items():
            if request.url.path == path:
                if isinstance(payload, httpx.Response):
                    return payload
                return httpx.Response(200, json=payload, request=request)
        return httpx.Response(404, request=request)

    return handler


def test_app_api_rankings_signed_request_and_host_fallback() -> None:
    seen: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.host == "down.test":
            raise httpx.ConnectError("boom", request=request)
        return httpx.Response(200, json=_json_fixture("javdb_api_rankings_daily.json"), request=request)

    async def run() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            api = JavDBAppApi(client, hosts=("https://down.test", "https://up.test"), clock=lambda: 1700000000)
            return await api.rankings("uncensored", "week")

    movies = asyncio.run(run())
    assert len(movies) == 3
    request = seen[-1]
    assert request.url.host == "up.test" and request.url.path == "/api/v1/rankings"
    assert request.url.params["type"] == "1" and request.url.params["period"] == "weekly"
    assert request.headers["jdsignature"] == sign(1700000000)


def test_app_api_auth_envelope_raises_auth_required() -> None:
    seen: list[httpx.Request] = []
    handler = _api_handler(
        {"/api/v1/movies/top": {"success": 0, "action": "JWTVerificationError", "message": "token required"}}, seen
    )

    async def run() -> None:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            await JavDBAppApi(client, hosts=("https://api.test",)).top250()

    with pytest.raises(JavDBApiAuthRequired):
        asyncio.run(run())


RANKINGS_HTML = """<html><body><nav><a href="/rankings/movies?p=daily&t=censored">有碼</a></nav>
<div class="movie-list"><div class="item"><a href="/v/AbC12" class="box" title="t">
<div class="video-title"><strong>SONE-001</strong> HTML title</div></a></div></div></body></html>"""


def _discover_with(api_handler, html_handler) -> tuple[DiscoverService, list[httpx.AsyncClient]]:
    api_client = httpx.AsyncClient(transport=httpx.MockTransport(api_handler))
    html_client = httpx.AsyncClient(transport=httpx.MockTransport(html_handler))
    api = JavDBAppApi(api_client, hosts=("https://api.test",))
    javdb = JavDBProvider(html_client, "https://javdb.test")
    return DiscoverService(ProviderRegistry([]), javdb, None, javdb_api=api), [api_client, html_client]


def test_browse_rankings_prefers_app_api(tmp_path: Path) -> None:
    html_calls: list[httpx.Request] = []

    async def html_handler(request: httpx.Request) -> httpx.Response:
        html_calls.append(request)
        return httpx.Response(200, text=RANKINGS_HTML, request=request)

    api_handler = _api_handler({"/api/v1/rankings": _json_fixture("javdb_api_rankings_daily.json")}, [])

    async def run() -> Any:
        discover, clients = _discover_with(api_handler, html_handler)
        try:
            with _database(tmp_path).session() as session:
                return await discover.browse(Repository(session), provider="javdb", list_name="rankings_daily")
        finally:
            for client in clients:
                await client.aclose()

    page = asyncio.run(run())
    assert page.source == "javdb_app_api"
    assert [item.code for item in page.items] == ["IPZZ-937", "IPZZ-961", "MIDA-837"]
    assert page.items[0].source_url.endswith("/v/Mb0JkJ")
    assert html_calls == []


def test_browse_rankings_falls_back_to_html_when_api_fails(tmp_path: Path) -> None:
    async def html_handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/rankings/movies"
        return httpx.Response(200, text=RANKINGS_HTML, request=request)

    async def api_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, request=request)

    async def run() -> Any:
        discover, clients = _discover_with(api_handler, html_handler)
        try:
            with _database(tmp_path).session() as session:
                return await discover.browse(
                    Repository(session), provider="javdb", list_name="rankings_weekly_uncensored"
                )
        finally:
            for client in clients:
                await client.aclose()

    page = asyncio.run(run())
    assert page.source == "javdb_html" and page.note and "app API failed" in page.note
    assert [item.code for item in page.items] == ["SONE-001"]


def test_top250_without_token_is_a_clear_error(tmp_path: Path) -> None:
    api_handler = _api_handler(
        {"/api/v1/movies/top": {"success": 0, "action": "JWTVerificationError", "message": "x"}}, []
    )

    async def html_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError("top250 has no HTML fallback")

    async def run() -> None:
        discover, clients = _discover_with(api_handler, html_handler)
        try:
            with _database(tmp_path).session() as session:
                await discover.browse(Repository(session), provider="javdb", list_name="top250")
        finally:
            for client in clients:
                await client.aclose()

    with pytest.raises(ValueError, match="TOKEN"):
        asyncio.run(run())


def test_daily_chart_does_not_ask_fanza_for_javdb_only_lists(tmp_path: Path) -> None:
    class _Discover:
        def __init__(self) -> None:
            self.calls: list[tuple[str, str]] = []

        async def browse(self, repo: Any, *, provider: str, list_name: str, page: int) -> Any:
            self.calls.append((provider, list_name))
            raise ValueError("unsupported")

    fake = _Discover()
    asyncio.run(
        collect_browse_pages(
            fake,  # type: ignore[arg-type]
            None,  # type: ignore[arg-type]
            provider=("javdb", "fanza"),
            lists=("rankings_daily", "rankings_daily_fc2", "top250"),
        )
    )
    assert ("fanza", "rankings_daily") in fake.calls
    assert ("fanza", "rankings_daily_fc2") not in fake.calls
    assert ("fanza", "top250") not in fake.calls
    assert ("javdb", "top250") in fake.calls


def test_parse_javdb_list_ignores_non_movie_links_and_uses_strong_code() -> None:
    items = parse_javdb_list(RANKINGS_HTML, "https://javdb.test")
    assert [(item.external_id, item.code) for item in items] == [("AbC12", "SONE-001")]
    assert javdb_movie_id_from_url("https://javdb.com/v/AbC12?x=1") == "AbC12"
    assert javdb_movie_id_from_url("https://javdb.com/actors/AbC12") is None


# --- magnet quality (JHS) ---------------------------------------------------------


def test_magnet_flags_cd_suffix_is_not_subtitle() -> None:
    assert magnet_quality_flags("SONE-001-C.mp4") == (True, False)
    assert magnet_quality_flags("SONE-001-cd1") == (False, False)
    assert magnet_quality_flags("SONE-001 1080p")[1] is True


def test_magnet_score_penalises_samples_and_tiny_files() -> None:
    full = magnet_quality_score(name="SONE-001 1080p", size_bytes=5 << 30, has_subtitle=False, hd=False)
    sample = magnet_quality_score(name="SONE-001 sample 1080p", size_bytes=5 << 30, has_subtitle=False, hd=False)
    tiny = magnet_quality_score(name="SONE-001 1080p", size_bytes=100 << 20, has_subtitle=False, hd=False)
    assert full > tiny > sample


def _magnet(name: str, size: int, subtitle: bool = False) -> MagnetLink:
    digest = hashlib.sha1(name.encode()).hexdigest().upper()
    return MagnetLink(
        provider="t", info_hash=digest, uri=f"magnet:?xt=urn:btih:{digest}", name=name, size_bytes=size,
        has_subtitle=subtitle,
    )


def test_rank_magnets_prefers_subtitled_4k_over_bigger_plain() -> None:
    ranked = rank_magnets(
        [_magnet("A-1", 9 << 30), _magnet("A-1-C 4K", 6 << 30), _magnet("A-1 trailer", 9 << 30)]
    )
    assert ranked[0].name == "A-1-C 4K" and ranked[-1].name == "A-1 trailer"


def test_pick_best_magnet_uses_quality_score() -> None:
    def row(name: str, size: int, subtitle: bool = False) -> WorkMagnet:
        return WorkMagnet(
            work_id="w", provider="t", info_hash=hashlib.sha1(name.encode()).hexdigest(), uri="magnet:?x",
            name=name, size_bytes=size, has_subtitle=subtitle, hd=False,
        )

    best = pick_best_magnet([row("A-1 sample", 20 << 30), row("A-1-C", 4 << 30, True), row("A-1", 5 << 30)])
    assert best is not None and best.name == "A-1-C"


# --- r18 resumable download (javinizer-go) -----------------------------------------

DUMP_URL = "https://dump.test/r18dev_dump_2026-09-29.sql.gz"
PAYLOAD = gzip.compress(b"INSERT INTO t VALUES (1);\n" * 20000)
ETAG = '"abc123"'


class _TruncatedStream(httpx.SyncByteStream):
    def __init__(self, data: bytes, cut: int) -> None:
        self._data, self._cut = data, cut

    def __iter__(self) -> Iterator[bytes]:
        yield self._data[: self._cut]
        raise httpx.ReadError("connection reset")


def _dump_transport(*, cut: int | None, range_status: int = 206, blocked: bool = False) -> tuple[httpx.MockTransport, list[httpx.Request]]:
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/dumps/latest":
            if blocked:
                return httpx.Response(403, text=CF_PAGE, headers={"content-type": "text/html"}, request=request)
            return httpx.Response(302, headers={"location": DUMP_URL}, request=request)
        headers = {"etag": ETAG, "accept-ranges": "bytes", "content-type": "application/gzip"}
        range_header = request.headers.get("range")
        if range_header:
            assert request.headers.get("if-range") == ETAG
            start = int(range_header.split("=")[1].rstrip("-"))
            if range_status == 200:
                return httpx.Response(200, stream=httpx.ByteStream(PAYLOAD), headers=headers, request=request)
            headers["content-range"] = f"bytes {start}-{len(PAYLOAD) - 1}/{len(PAYLOAD)}"
            return httpx.Response(206, stream=httpx.ByteStream(PAYLOAD[start:]), headers=headers, request=request)
        headers["content-length"] = str(len(PAYLOAD))
        if cut is not None:
            return httpx.Response(200, stream=_TruncatedStream(PAYLOAD, cut), headers=headers, request=request)
        return httpx.Response(200, stream=httpx.ByteStream(PAYLOAD), headers=headers, request=request)

    return httpx.MockTransport(handler), seen


def _download(tmp_path: Path, transport: httpx.MockTransport) -> Any:
    with httpx.Client(transport=transport) as client:
        return download_latest_dump(
            tmp_path, client=client, latest_url="https://r18.test/dumps/latest", sleep=lambda _s: None
        )


def test_r18_download_resumes_after_truncated_stream(tmp_path: Path) -> None:
    transport, seen = _dump_transport(cut=len(PAYLOAD) // 3)
    result = _download(tmp_path, transport)
    assert result.path.read_bytes() == PAYLOAD
    assert result.source_date == "2026-09-29"
    assert any(request.headers.get("range") for request in seen)
    assert not list(tmp_path.glob("*.part*"))


def test_r18_download_skips_complete_file(tmp_path: Path) -> None:
    transport, _seen = _dump_transport(cut=None)
    first = _download(tmp_path, transport)
    second = _download(tmp_path, transport)
    assert first.unchanged is False and second.unchanged is True and second.bytes_downloaded == 0


def test_r18_download_resumes_from_existing_part_file(tmp_path: Path) -> None:
    transport, _seen = _dump_transport(cut=len(PAYLOAD) // 2)
    with pytest.raises(R18DumpDownloadError):
        with httpx.Client(transport=transport) as client:
            download_latest_dump(
                tmp_path, client=client, latest_url="https://r18.test/dumps/latest",
                sleep=lambda _s: None, max_resume_attempts=0,
            )
    assert (tmp_path / "r18dev_dump_2026-09-29.sql.gz.part").is_file()
    transport, _seen = _dump_transport(cut=10)
    result = _download(tmp_path, transport)
    assert result.resumed is True and result.path.read_bytes() == PAYLOAD


def test_r18_download_refuses_range_ignored(tmp_path: Path) -> None:
    transport, _seen = _dump_transport(cut=100, range_status=200)
    with pytest.raises(R18DumpDownloadError, match="ignored the Range"):
        _download(tmp_path, transport)


def test_r18_download_reports_cloudflare_block(tmp_path: Path) -> None:
    transport, _seen = _dump_transport(cut=None, blocked=True)
    with pytest.raises(R18DumpDownloadError, match="Cloudflare"):
        _download(tmp_path, transport)


def test_parse_content_range() -> None:
    assert parse_content_range("bytes 10-99/100") == (10, 100)
    assert parse_content_range("bytes 10-99/*") == (10, None)
    with pytest.raises(R18DumpDownloadError):
        parse_content_range("items 1-2/3")


# --- DMM content-id candidates (javinizer-go content_id_prefixes) -------------------


def test_known_prefix_table_and_candidates() -> None:
    assert "118" in known_content_id_prefixes("abf")
    candidates = content_id_candidates("NPS-472", limit=6)
    assert "h_021nps00472" in candidates
    assert len(candidates) <= 6
    assert content_id_candidates("SONE-001")[0] in {"sone00001", "sone001"}


# --- actress alias merge (javinizer-go actress_merger) -----------------------------


def test_merge_actor_names_dedups_romaji_and_japanese() -> None:
    table = ActressNameMap.from_mapping({"Mikami Yua": "三上悠亜"})
    assert merge_actor_names(["Yua Mikami"], ["三上悠亜"], name_map=table) == ["三上悠亜"]
    assert merge_actor_names(["三上悠亜"], ["Yua Mikami", "河北彩花"], name_map=table) == ["三上悠亜", "河北彩花"]
    assert actor_identity_forms("Mikami Yua", table) & actor_identity_forms("Yua  Mikami", table)


# --- MGStage label prefixes (javinizer-go mgstage) ---------------------------------

MGSTAGE_DETAIL = """<html><body><div class="common_detail_cover"><h1>Fixture MG title</h1></div>
<div class="detail_left"><table><tr><th>品番：</th><td>200GANA-2850</td></tr>
<tr><th>出演：</th><td><a href="/x">Actress A</a></td></tr></table></div></body></html>"""
MGSTAGE_LANDING = "<html><body><h1>MGS動画</h1></body></html>"


def test_mgstage_candidates() -> None:
    assert mgstage_product_candidates("gana-2850") == ("GANA-2850", "200GANA-2850")
    assert mgstage_product_candidates("ABP-420") == ("ABP-420",)


def test_mgstage_tries_label_prefix_and_keeps_requested_code() -> None:
    requested: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.path)
        body = MGSTAGE_DETAIL if "200GANA-2850" in request.url.path else MGSTAGE_LANDING
        return httpx.Response(200, text=body, request=request)

    async def run() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            provider = MgstageProvider(client, "https://mg.test")
            return await provider.search(IdentityHints(term="GANA-2850", mode=QueryMode.CODE, code="GANA-2850"))

    records = asyncio.run(run())
    assert requested == ["/product/product_detail/GANA-2850/", "/product/product_detail/200GANA-2850/"]
    assert len(records) == 1 and records[0].code == "GANA-2850"


# --- JavBus info panel scoping (javinizer-go javbus) -------------------------------

JAVBUS_REAL = """<html><body>
<nav><a href="https://www.javbus.com/star/navstar">人氣女優</a><a href="/genre/nav">類別</a></nav>
<div class="container"><h3>SONE-001 Real title</h3>
<div class="row movie"><div class="col-md-9 screencap"><a class="bigImage" href="/pics/cover/x_b.jpg"><img src="/pics/cover/x_b.jpg"></a></div>
<div class="col-md-3 info">
<p><span class="header">識別碼:</span> <span style="color:#CC0000;">SONE-001</span></p>
<p><span class="header">發行日期:</span> 2024-01-02</p>
<p><span class="header">長度:</span> 120分鐘</p>
<p><span class="header">製作商:</span> <a href="/studio/1">S1 NO.1 STYLE</a></p>
<p><span class="genre"><label><a href="/genre/a">單體作品</a></label></span></p>
<div class="star-name"><a href="/star/abc">河北彩花</a></div>
</div></div>
<div id="related-waterfall"><a class="movie-box" href="/OTHER-1"><date>OTHER-1</date><date>2019-05-05</date>
<a href="/star/zzz">Other Star</a></a></div>
</div></body></html>"""


def test_javbus_detail_scoped_to_info_panel() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "search" in request.url.path:
            return httpx.Response(200, text='<a class="movie-box" href="/SONE-001">x</a>', request=request)
        return httpx.Response(200, text=JAVBUS_REAL, request=request)

    async def run() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JavBusProvider(client, "https://bus.test").search(
                IdentityHints(term="SONE-001", mode=QueryMode.CODE, code="SONE-001")
            )

    (record,) = asyncio.run(run())
    assert record.code == "SONE-001"
    assert record.actors == ("河北彩花",)
    assert record.tags == ("單體作品",)
    assert record.studio == "S1 NO.1 STYLE"
    assert record.release_date is not None and record.release_date.isoformat() == "2024-01-02"


def test_javbus_falls_back_to_uncensored_search() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.headers.get("cookie") == "existmag=all; dv=1"
        if request.url.path.startswith("/uncensored/search"):
            return httpx.Response(200, text='<a class="movie-box" href="/SONE-001">x</a>', request=request)
        if request.url.path.startswith("/search"):
            return httpx.Response(200, text="<html>no results</html>", request=request)
        return httpx.Response(200, text=JAVBUS_REAL, request=request)

    async def run() -> list[Any]:
        async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
            return await JavBusProvider(client, "https://bus.test").search(
                IdentityHints(term="SONE-001", mode=QueryMode.CODE, code="SONE-001")
            )

    assert len(asyncio.run(run())) == 1
    assert paths[:2] == ["/search/SONE-001", "/uncensored/search/SONE-001"]


# --- DMM awsimgsrc cover upgrade (javinizer-go dmm) ---------------------------------


def test_dmm_awsimgsrc_mapping() -> None:
    assert (
        dmm_awsimgsrc_url("https://pics.dmm.co.jp/digital/video/sone00001/sone00001pl.jpg")
        == "https://awsimgsrc.dmm.com/dig/digital/video/sone00001/sone00001pl.jpg"
    )
    assert dmm_awsimgsrc_url("https://pics.dmm.co.jp/mono/actjpgs/someone.jpg") is None
    assert dmm_awsimgsrc_url("https://example.com/digital/video/a/apl.jpg") is None


# --- 115 error codes (p115client) --------------------------------------------------


def test_pan_error_code_fields_and_state_zero() -> None:
    assert api_error_code({"state": False, "errno": 20018}) == 20018
    assert api_error_code({"state": 0, "code": 40140125}) == 40140125
    assert api_error_code({"state": True, "code": 0}) is None
    with pytest.raises(PanApiError) as caught:
        _check_api_ok({"state": 0, "errno": 40140116, "message": "dead"}, context="x")
    assert caught.value.code == 40140116


def test_pan_file_gone_codes_match_p115client() -> None:
    assert {20013, 20018, 31003, 50015, 70005, 70008, 90008, 430004} <= FILE_GONE_CODES
    assert 70004 not in FILE_GONE_CODES  # "upload incomplete", not gone


def test_artwork_prefers_awsimgsrc_and_falls_back_on_404(tmp_path: Path) -> None:
    from shadow_mdc.media.artwork import ArtworkStore

    jpeg = b"\xff\xd8\xff\xe0" + b"0" * 64
    seen: list[str] = []

    def make_handler(mirror_status: int):
        async def handler(request: httpx.Request) -> httpx.Response:
            seen.append(request.url.host)
            if request.url.host == "awsimgsrc.dmm.com" and mirror_status != 200:
                return httpx.Response(mirror_status, request=request)
            return httpx.Response(200, content=jpeg, headers={"content-type": "image/jpeg"}, request=request)

        return handler

    url = "https://pics.dmm.co.jp/digital/video/sone00001/sone00001pl.jpg"

    async def run(status: int, root: Path) -> Path:
        async with httpx.AsyncClient(transport=httpx.MockTransport(make_handler(status))) as client:
            store = ArtworkStore(root, client, max_bytes=1 << 20)
            root.mkdir(parents=True, exist_ok=True)
            path, _cached = await store._acquire_url(root, "thumb", url, existing=None)
            return path

    assert asyncio.run(run(200, tmp_path / "a")).read_bytes() == jpeg
    assert seen == ["awsimgsrc.dmm.com"]
    seen.clear()
    assert asyncio.run(run(404, tmp_path / "b")).read_bytes() == jpeg
    assert seen == ["awsimgsrc.dmm.com", "pics.dmm.co.jp"]
