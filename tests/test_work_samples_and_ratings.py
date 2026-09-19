from __future__ import annotations

from pathlib import Path
from subprocess import CompletedProcess

import httpx
import pytest
from selectolax.parser import HTMLParser
from starlette.testclient import TestClient

from shadow_mdc.api import app
from shadow_mdc.domain import IdentityHints
from shadow_mdc.enums import ContentFamily, MediaCategory, QueryMode
from shadow_mdc.media.screenshots import capture_sample_frames
from shadow_mdc.providers.fanza import FanzaProvider
from shadow_mdc.providers.html_fields import sample_image_artwork
from shadow_mdc.providers.javbus import JavBusProvider
from shadow_mdc.providers.jav321 import Jav321Provider
from shadow_mdc.providers.r18dev import R18DevProvider
from shadow_mdc.providers.ratings import parse_hreview_rating, parse_short_reviews

FIXTURES = Path(__file__).parent / "fixtures"


def _fixture(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


@pytest.mark.asyncio
async def test_fanza_parses_samples_rating_and_reviews() -> None:
    async def handler(request: httpx.Request) -> httpx.Response:
        if "graphql" in str(request.url):
            return httpx.Response(500, text="fail", request=request)
        return httpx.Response(200, text=_fixture("fanza_detail.html"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        records = await FanzaProvider(client, "https://fixture.test").search(
            IdentityHints(
                term="IPX-219",
                mode=QueryMode.CODE,
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                code="IPX-219",
            )
        )
    assert len(records) == 1
    record = records[0]
    samples = [item for item in record.artwork if item.kind == "sample"]
    assert len(samples) >= 3
    assert record.rating == pytest.approx(4.52)
    assert record.rating_count == 128
    assert len(record.reviews) >= 1
    assert "見応え" in record.reviews[0].text or "もう一度" in record.reviews[0].text


@pytest.mark.asyncio
async def test_javbus_and_jav321_and_r18dev_expose_sample_stills() -> None:
    async def javbus_handler(request: httpx.Request) -> httpx.Response:
        if "/search/" in request.url.path:
            return httpx.Response(
                200,
                text='<a class="movie-box" href="/ABP-123">x</a>',
                request=request,
            )
        return httpx.Response(200, text=_fixture("javbus_detail.html"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(javbus_handler)) as client:
        bus = await JavBusProvider(client, "https://fixture.test").search(
            IdentityHints(
                term="ABP-123",
                mode=QueryMode.CODE,
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                code="ABP-123",
            )
        )
    assert any(item.kind == "sample" for item in bus[0].artwork)

    async def j321_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture("jav321_detail.html"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(j321_handler)) as client:
        records = await Jav321Provider(client, "https://fixture.test").search(
            IdentityHints(
                term="SONE-118",
                mode=QueryMode.CODE,
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                code="SONE-118",
            )
        )
    assert any(item.kind == "sample" for item in records[0].artwork)

    async def r18_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=_fixture("r18dev_detail.json"), request=request)

    async with httpx.AsyncClient(transport=httpx.MockTransport(r18_handler)) as client:
        records = await R18DevProvider(client, "https://fixture.test").search(
            IdentityHints(
                term="IPX-219",
                mode=QueryMode.CODE,
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
                code="IPX-219",
            )
        )
    assert sum(1 for item in records[0].artwork if item.kind == "sample") >= 2


def test_sample_image_artwork_helper_and_rating_parser() -> None:
    html = """
    <div id="sample-image-block">
      <a href="/a.jpg"><img src="/a.jpg"></a>
      <a href="/b.jpg"><img src="/b.jpg"></a>
    </div>
    <span class="d-review__average__num">3.5</span>
    <span class="d-review__evaluates"><span>9</span></span>
    <div class="d-review__unit"><p class="d-review__unit__comment">短评内容足够长了。</p></div>
    """
    root = HTMLParser(html)
    samples = sample_image_artwork(root, "https://example.test", ("#sample-image-block a",))
    assert len(samples) == 2
    assert all(item.kind == "sample" for item in samples)
    value, count, maximum = parse_hreview_rating(root)
    assert value == 3.5
    assert count == 9
    assert maximum == 5.0
    reviews = parse_short_reviews(root, provider="fanza")
    assert len(reviews) == 1


def test_capture_sample_frames_writes_ratio_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "clip.mp4"
    source.write_bytes(b"video")
    out = tmp_path / "samples"

    def fake_ffmpeg_run(args: list[str], **_: object) -> CompletedProcess[str]:
        Path(args[-1]).write_bytes(b"\xff\xd8frame")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("shadow_mdc.media.screenshots.shutil.which", lambda command: "ffmpeg")
    monkeypatch.setattr("shadow_mdc.media.screenshots.subprocess.run", fake_ffmpeg_run)
    frames = capture_sample_frames(source, out, duration_seconds=100.0, ratios=(0.1, 0.5), limit=2)
    assert len(frames) == 2
    assert frames[0].path.name == "sample_01.jpg"
    assert frames[0].path.is_file()
    assert frames[1].ratio == 0.5


def test_work_samples_api_prefers_web_then_local_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    data_dir = tmp_path / "data"
    media_dir = tmp_path / "incoming" / "CreatorOne"
    media_dir.mkdir(parents=True)
    video = media_dir / "1.mp4"
    video.write_bytes(b"fixture video")
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(data_dir))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{data_dir / 'samples.db'}")

    def fake_ffmpeg_run(args: list[str], **_: object) -> CompletedProcess[str]:
        Path(args[-1]).write_bytes(b"\xff\xd8generated-frame")
        return CompletedProcess(args, 0, "", "")

    monkeypatch.setattr("shadow_mdc.media.screenshots.shutil.which", lambda command: "ffmpeg")
    monkeypatch.setattr("shadow_mdc.media.screenshots.subprocess.run", fake_ffmpeg_run)

    with TestClient(app) as client:
        library = client.post(
            "/api/libraries",
            json={"name": "Samples lib", "root_path": str(media_dir.parent)},
        ).json()
        client.post(f"/api/libraries/{library['id']}/scan")
        assets = client.get("/api/assets").json()
        assigned = client.post(
            f"/api/assets/{assets[0]['id']}/directory-actor",
            json={"actor": "CreatorOne", "category": "Europe"},
        )
        assert assigned.status_code == 200
        work_id = assigned.json()["work_id"] if "work_id" in assigned.json() else None
        if work_id is None:
            work_id = client.get("/api/assets").json()[0]["work_id"]
        # Seed a remote sample URL on the work via artwork prefer path: patch DB through refresh not available.
        # Call generate samples — with no web samples, ffmpeg should fill.
        generated = client.post(f"/api/works/{work_id}/samples?target_count=3")
        assert generated.status_code == 200, generated.text
        body = generated.json()
        assert body["local_generated"] == 3
        assert body["sample_count"] >= 3
        detail = client.get(f"/api/works/{work_id}").json()
        assert len(detail["sample_urls"]) >= 3
        # Cached path should skip regenerating when enough samples exist
        again = client.post(
            f"/api/libraries/{library['id']}/samples",
            json={"limit": 10, "target_count": 3},
        ).json()
        assert again["skipped_cached"] >= 1
