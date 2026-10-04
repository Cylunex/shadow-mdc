"""STRM play relay: ``/api/strm/play/{file_id}`` → 302 to a 115 media URL.

Resolve order (idea from peer notes; own implementation): file id → pick code →
**direct download URL first** (seekable CDN link) → only if that fails, the
play/transcode URL. The 115 link is requested with the same User-Agent the
player will use to follow the redirect (115 binds links to the UA), falling
back to one configured default UA when the player sends none.

Quota split: only the pick code → ``downurl`` call is meant to spend 115 API
quota on playback. Pick codes are recorded in each export's sidecar, so the
relay resolves them from a local sidecar index first and only falls back to a
``folder/get_info`` API lookup for folders it has never exported.
"""

from __future__ import annotations

import asyncio
import hmac
import logging
import time
from collections import OrderedDict
from dataclasses import dataclass
from pathlib import Path

from .pan import DEFAULT_MEDIA_USER_AGENT, PanService
from .strm_export import iter_export_dirs, read_sidecar

logger = logging.getLogger(__name__)

URL_TTL_SECONDS = 10 * 60
PICK_CACHE_SIZE = 4096
URL_CACHE_SIZE = 1024
# Rebuild the sidecar pick-code index on a miss at most this often.
SIDECAR_INDEX_MIN_AGE = 10.0


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
        self._sidecar_picks: dict[str, str] = {}
        self._sidecar_root: str | None = None
        self._sidecar_built_at: float | None = None
        self.api_pick_lookups = 0

    def effective_user_agent(self, player_ua: str | None) -> str:
        ua = (player_ua or "").strip()
        if ua:
            return ua
        cfg = self._pan.config_store.load()
        return cfg.strm_user_agent or DEFAULT_MEDIA_USER_AGENT

    def _build_sidecar_index(self, root: str) -> dict[str, str]:
        index: dict[str, str] = {}
        for directory in iter_export_dirs(Path(root)):
            for entry in read_sidecar(directory):
                if entry.pick_code and not entry.is_openlist:
                    index[entry.file_id] = entry.pick_code
        return index

    async def _sidecar_pick(self, file_id: str) -> str | None:
        root = self._pan.config_store.load().strm_output_root
        if not root:
            return None
        if self._sidecar_root == root and file_id in self._sidecar_picks:
            return self._sidecar_picks[file_id]
        now = time.monotonic()
        fresh = (
            self._sidecar_root == root
            and self._sidecar_built_at is not None
            and now - self._sidecar_built_at < SIDECAR_INDEX_MIN_AGE
        )
        if fresh:
            return None
        try:
            index = await asyncio.to_thread(self._build_sidecar_index, root)
        except OSError:
            return None
        self._sidecar_picks = index
        self._sidecar_root = root
        self._sidecar_built_at = time.monotonic()
        return index.get(file_id)

    async def _pick_code(self, file_id: str) -> str:
        cached = self._picks.get(file_id)
        if cached:
            self._picks.move_to_end(file_id)
            return cached
        local = await self._sidecar_pick(file_id)
        if local:
            self._remember_pick(file_id, local)
            return local
        self.api_pick_lookups += 1
        client = self._pan.get_client()
        info = await client.get_folder_info(file_id)
        pick = info.get("pick_code") or info.get("pc")
        if not pick:
            raise RelayError(404, "115 file not found")
        self._remember_pick(file_id, str(pick))
        return str(pick)

    def _remember_pick(self, file_id: str, pick: str) -> None:
        self._picks[file_id] = pick
        while len(self._picks) > PICK_CACHE_SIZE:
            self._picks.popitem(last=False)

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
