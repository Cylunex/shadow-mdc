"""STRM play relay: ``/api/strm/play/{file_id}`` → 302 to a 115 media URL.

Resolve order (idea from peer notes; own implementation): file id → pick code →
**direct download URL first** (seekable CDN link) → only if that fails, the
play/transcode URL. The 115 link is requested with the same User-Agent the
player will use to follow the redirect (115 binds links to the UA), falling
back to one configured default UA when the player sends none.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass

from .pan import DEFAULT_MEDIA_USER_AGENT, PanService

logger = logging.getLogger(__name__)

URL_TTL_SECONDS = 10 * 60
PICK_CACHE_SIZE = 4096
URL_CACHE_SIZE = 1024


class RelayError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class RelayTarget:
    url: str
    source: str  # "download" | "play"


def token_ok(expected: str | None, supplied: str | None) -> bool:
    if not expected:
        return True
    if not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


class StrmRelay:
    def __init__(self, pan: PanService, *, url_ttl: float = URL_TTL_SECONDS) -> None:
        self._pan = pan
        self._url_ttl = url_ttl
        self._picks: OrderedDict[str, str] = OrderedDict()
        self._urls: OrderedDict[tuple[str, str], tuple[float, RelayTarget]] = OrderedDict()
        self._locks: dict[str, asyncio.Lock] = {}

    def effective_user_agent(self, player_ua: str | None) -> str:
        ua = (player_ua or "").strip()
        if ua:
            return ua
        cfg = self._pan.config_store.load()
        return cfg.strm_user_agent or DEFAULT_MEDIA_USER_AGENT

    async def _pick_code(self, file_id: str) -> str:
        cached = self._picks.get(file_id)
        if cached:
            self._picks.move_to_end(file_id)
            return cached
        client = self._pan.get_client()
        info = await client.get_folder_info(file_id)
        pick = info.get("pick_code") or info.get("pc")
        if not pick:
            raise RelayError(404, "115 file not found")
        self._picks[file_id] = str(pick)
        while len(self._picks) > PICK_CACHE_SIZE:
            self._picks.popitem(last=False)
        return str(pick)

    async def resolve(self, file_id: str, player_ua: str | None) -> RelayTarget:
        if not self._pan.status().get("connected"):
            raise RelayError(503, "115 not connected")
        ua = self.effective_user_agent(player_ua)
        key = (file_id, ua)
        now = time.monotonic()
        hit = self._urls.get(key)
        if hit and hit[0] > now:
            return hit[1]
        lock = self._locks.setdefault(file_id, asyncio.Lock())
        async with lock:
            hit = self._urls.get(key)
            if hit and hit[0] > time.monotonic():
                return hit[1]
            try:
                pick = await self._pick_code(file_id)
            except RelayError:
                raise
            except Exception as exc:
                raise RelayError(502, f"115 lookup failed: {type(exc).__name__}") from exc
            client = self._pan.get_client()
            target: RelayTarget | None = None
            try:
                url = await client.download_url(pick, user_agent=ua)
                if url:
                    target = RelayTarget(url=url, source="download")
            except Exception as exc:
                logger.info("115 downurl failed (%s); trying play URL", type(exc).__name__)
            if target is None:
                try:
                    url = await client.video_play_url(pick, user_agent=ua)
                    if url:
                        target = RelayTarget(url=url, source="play")
                except Exception as exc:
                    raise RelayError(502, f"115 play URL failed: {type(exc).__name__}") from exc
            if target is None:
                raise RelayError(502, "115 returned no playable URL")
            self._urls[key] = (time.monotonic() + self._url_ttl, target)
            while len(self._urls) > URL_CACHE_SIZE:
                self._urls.popitem(last=False)
            return target
