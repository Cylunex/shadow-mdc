from pathlib import Path

import httpx
import pytest

from shadow_mdc.db.models import Work
from shadow_mdc.media.artwork import ArtworkStore


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
