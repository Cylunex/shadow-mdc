from pathlib import Path

import httpx
import pytest
from PIL import Image, ImageDraw

from shadow_mdc.db.models import Work
from shadow_mdc.media.artwork import ArtworkStore, package_portrait, resolve_work_display_image


@pytest.mark.asyncio
async def test_artwork_store_downloads_once_and_reuses_hash_cache(tmp_path: Path) -> None:
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            content=b"jpeg-fixture",
            headers={"content-type": "image/jpeg"},
            request=request,
        )

    work = Work(
        id="work-id",
        title="Fixture",
        artwork=[{"url": "https://images.example/poster.jpg", "kind": "thumb"}],
    )
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        store = ArtworkStore(tmp_path, client, max_bytes=1024)
        first, paths = await store.acquire(work)
        second, second_paths = await store.acquire(work)

    assert first.downloaded == 1
    assert second.cached == 1
    assert requests == 1
    assert paths == second_paths
    cached_path = Path(next(iter(paths.values())))
    assert cached_path == tmp_path / "work-id" / "poster.jpg"
    assert cached_path.read_bytes() == b"jpeg-fixture"


@pytest.mark.asyncio
async def test_artwork_store_rejects_private_addresses(tmp_path: Path) -> None:
    work = Work(
        id="work-id",
        title="Fixture",
        artwork=[{"url": "http://127.0.0.1/private.jpg", "kind": "thumb"}],
    )
    async with httpx.AsyncClient() as client:
        result, paths = await ArtworkStore(tmp_path, client, max_bytes=1024).acquire(work)

    assert result.failed == 1
    assert paths == {}


@pytest.mark.asyncio
async def test_derive_portrait_only_with_matching_reference_and_keep_manual_cover(tmp_path: Path) -> None:
    artwork = tmp_path / "artwork"
    root = artwork / "cover"
    root.mkdir(parents=True)
    front = Image.new("RGB", (400, 600), "orange")
    ImageDraw.Draw(front).rectangle((80, 140, 315, 520), fill="navy")
    package = Image.new("RGB", (830, 600), "lime")
    package.paste(front, (430, 0))
    package.save(root / "fanart.jpg")
    front.resize((160, 240)).save(root / "poster.jpg")
    work = Work(
        id="cover", title="Fixture", artwork=[
            {"url": "https://example.com/thumb.jpg", "kind": "poster"},
            {"url": "https://example.com/pack.jpg", "kind": "fanart"},
        ],
    )
    store = ArtworkStore(artwork, None, max_bytes=1_000_000)
    await store.acquire(work)
    derived = package_portrait(work, root)
    assert derived is not None
    with Image.open(derived) as image:
        assert image.size == (400, 600)
    image_url = resolve_work_display_image(work, data_dir=tmp_path)
    assert image_url is not None and image_url.endswith("/package-portrait")
    with Image.open(root / "fanart.jpg") as image:
        assert image.size == (830, 600)
    work.field_locks = ["artwork"]
    assert package_portrait(work, root) is None
    work.field_locks = []
    package.paste(front, (0, 0))
    package.save(root / "fanart.jpg")
    await store.acquire(work)
    assert package_portrait(work, root) is None
