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

Signed URL cache: short TTL keyed by authVersion+account+directory+fileID+UA;
honour 115 ``t``/``expires`` (-30s margin); never cache unknown expiry; cap 256;
singleflight coalesce so one caller's cancel cannot poison another's resolve.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import tempfile
import time
from collections import OrderedDict
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from .offline_recovery import stream_cache_expiry
from .pan import DEFAULT_MEDIA_USER_AGENT, PanService, SourceChangedError
from .pan_common import SingleFlight
from .strm_export import SIDECAR_NAME, export_revision, export_source_scope, iter_export_dirs, read_sidecar, read_sidecar_scope

logger = logging.getLogger(__name__)

PICK_CACHE_SIZE = 4096
URL_CACHE_SIZE = 256
# Rebuild the sidecar pick-code index on a miss at most this often.
SIDECAR_INDEX_MIN_AGE = 10.0
RESOLVE_TIMEOUT_SECONDS = 45.0
_ManifestStat = tuple[int, int, int, int, int]
_IndexedPick = tuple[str, str, _ManifestStat]


class RelayError(Exception):
    def __init__(self, status_code: int, message: str) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.message = message


@dataclass(frozen=True, slots=True)
class RelayTarget:
    url: str
    source: str  # "download" | "play"
    expires_at: float | None = None  # monotonic deadline when known


def token_ok(expected: str | None, supplied: str | None) -> bool:
    if not expected:
        return True
    if not supplied:
        return False
    return hmac.compare_digest(expected.encode("utf-8"), supplied.encode("utf-8"))


class StrmRelay:
    def __init__(self, pan: PanService, *, index_dir: Path | None = None) -> None:
        self._pan = pan
        self._index_dir = index_dir
        self._picks: OrderedDict[tuple[str, int, str, str], str] = OrderedDict()
        # key → (monotonic_expires, target); only entries with known URL expiry.
        self._urls: OrderedDict[str, tuple[float, RelayTarget]] = OrderedDict()
        self._flight: SingleFlight[RelayTarget] = SingleFlight()
        self._sidecar_picks: dict[str, _IndexedPick] = {}
        self._sidecar_generation: tuple[str, str, int] | None = None
        self._sidecar_lock = asyncio.Lock()
        self._sidecar_built_at: float | None = None
        self.api_pick_lookups = 0

    def effective_user_agent(self, player_ua: str | None) -> str:
        ua = (player_ua or "").strip()
        if ua:
            return ua
        cfg = self._pan.config_store.load()
        return cfg.strm_user_agent or DEFAULT_MEDIA_USER_AGENT

    def cache_key(self, file_id: str, user_agent: str) -> str:
        fingerprint = self._pan.source_fingerprint()
        return "|".join(
            [
                fingerprint.backend,
                str(fingerprint.auth_version),
                fingerprint.account_id,
                fingerprint.directory_id,
                file_id,
                user_agent,
            ]
        )

    @staticmethod
    def _manifest_stat(root: Path, relative: str) -> _ManifestStat | None:
        manifest = root / relative / SIDECAR_NAME
        try:
            if manifest.is_symlink() or not manifest.resolve().is_relative_to(root):
                return None
            entry = manifest.stat()
            return entry.st_dev, entry.st_ino, entry.st_size, entry.st_mtime_ns, entry.st_ctime_ns
        except OSError:
            return None

    def _index_path(self, root: str, scope: str) -> Path | None:
        if self._index_dir is None:
            return None
        key = hashlib.sha256(json.dumps([root, scope]).encode()).hexdigest()
        return self._index_dir / f"{key}.json"

    def _read_sidecar_index(self, root: str, scope: str) -> dict[str, _IndexedPick]:
        path = self._index_path(root, scope)
        if path is None:
            return {}
        try:
            raw = json.loads(path.read_bytes())
            if raw.get("version") != 1:
                return {}
            return {key: (pick, relative, tuple(revision)) for key, (pick, relative, revision) in raw["picks"].items()}
        except (OSError, ValueError, TypeError, AttributeError, KeyError):
            return {}

    def _save_sidecar_index(self, root: str, scope: str, index: dict[str, _IndexedPick]) -> None:
        path = self._index_path(root, scope)
        if path is None:
            return
        temporary = None
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=path.parent, mode="w", encoding="utf-8", delete=False) as target:
                temporary = Path(target.name)
                json.dump({"version": 1, "picks": index}, target, ensure_ascii=False)
            os.replace(temporary, path)
        except OSError:
            if temporary is not None:
                temporary.unlink(missing_ok=True)

    def _build_sidecar_index(self, root: str, scope: str) -> dict[str, _IndexedPick]:
        export_root = Path(root)
        index: dict[str, _IndexedPick] = {}
        for directory in iter_export_dirs(export_root):
            relative = directory.relative_to(export_root).as_posix()
            revision = self._manifest_stat(export_root, relative)
            if revision is None or read_sidecar_scope(directory) != scope:
                continue
            entries = read_sidecar(directory)
            if self._manifest_stat(export_root, relative) != revision:
                continue
            for entry in entries:
                if entry.pick_code and not entry.is_openlist:
                    index[entry.file_id] = (entry.pick_code, relative, revision)
        return index

    def _indexed_pick(self, file_id: str, root: str) -> str | None:
        entry = self._sidecar_picks.get(file_id)
        if entry is None:
            return None
        pick, relative, revision = entry
        if revision == self._manifest_stat(Path(root), relative):
            return pick
        self._sidecar_picks.pop(file_id, None)
        return None

    async def _sidecar_pick(self, file_id: str, scope: str, root: str) -> str | None:
        if not root:
            return None
        root = str(Path(root).resolve())
        async with self._sidecar_lock:
            generation = (root, scope, export_revision())
            same = generation == self._sidecar_generation
            if self._sidecar_generation is None or self._sidecar_generation[:2] != generation[:2]:
                self._sidecar_picks = await asyncio.to_thread(self._read_sidecar_index, root, scope)
                self._sidecar_generation = generation
                self._sidecar_built_at = None
                same = True
            local = await asyncio.to_thread(self._indexed_pick, file_id, root)
            if local:
                return local
            if same and self._sidecar_built_at is not None and time.monotonic() - self._sidecar_built_at < SIDECAR_INDEX_MIN_AGE:
                return None
            try:
                index = await asyncio.to_thread(self._build_sidecar_index, root, scope)
            except OSError:
                return None
            await asyncio.to_thread(self._save_sidecar_index, root, scope, index)
            self._sidecar_picks = index
            self._sidecar_generation = generation
            self._sidecar_built_at = time.monotonic()
            return await asyncio.to_thread(self._indexed_pick, file_id, root)

    async def _pick_code(self, file_id: str) -> str:
        source = self._pan.source_fingerprint()
        scope = export_source_scope(source)
        root = self._pan.config_store.load().strm_output_root or ""
        key = (scope, source.auth_version, root, file_id)
        cached = self._picks.get(key)
        if cached:
            self._picks.move_to_end(key)
            return cached
        local = await self._sidecar_pick(file_id, scope, root)
        if source != self._pan.source_fingerprint():
            raise SourceChangedError("pan source changed during STRM lookup")
        if local:
            return local
        self.api_pick_lookups += 1
        client = self._pan.get_client()
        info = await self._pan.read_after_check(lambda: client.get_folder_info(file_id))
        pick = info.get("pick_code") or info.get("pc")
        if not pick:
            raise RelayError(404, "115 file not found")
        self._remember_pick(key, str(pick))
        return str(pick)

    def _remember_pick(self, key: tuple[str, int, str, str], pick: str) -> None:
        self._picks[key] = pick
        while len(self._picks) > PICK_CACHE_SIZE:
            self._picks.popitem(last=False)

    def _cached_url(self, key: str) -> RelayTarget | None:
        now = time.monotonic()
        hit = self._urls.get(key)
        if hit is None:
            return None
        expires, target = hit
        if expires <= now:
            self._urls.pop(key, None)
            return None
        self._urls.move_to_end(key)
        return target

    def _remember_url(self, key: str, target: RelayTarget, wall_expires: datetime) -> None:
        # Convert wall-clock expiry to monotonic so sleep/NTP skew does not revive stale links.
        remaining = (wall_expires - datetime.now(UTC)).total_seconds()
        if remaining <= 0:
            return
        self._urls[key] = (time.monotonic() + remaining, target)
        while len(self._urls) > URL_CACHE_SIZE:
            self._urls.popitem(last=False)

    async def resolve(self, file_id: str, player_ua: str | None) -> RelayTarget:
        if not self._pan.status().get("connected"):
            raise RelayError(503, "115 not connected")
        ua = self.effective_user_agent(player_ua)
        key = self.cache_key(file_id, ua)
        hit = self._cached_url(key)
        if hit is not None:
            return hit

        async def factory() -> RelayTarget:
            # Re-check after winning the singleflight slot.
            cached = self._cached_url(key)
            if cached is not None:
                return cached
            try:
                async with asyncio.timeout(RESOLVE_TIMEOUT_SECONDS):
                    return await self._resolve_uncached(file_id, ua, key)
            except TimeoutError as exc:
                raise RelayError(504, "115 URL resolve timed out") from exc

        try:
            return await self._flight.run(key, factory)
        except SourceChangedError as exc:
            raise RelayError(409, "pan source changed during resolve") from exc
        except RelayError:
            raise
        except Exception as exc:
            raise RelayError(502, f"115 lookup failed: {type(exc).__name__}") from exc

    async def _resolve_uncached(self, file_id: str, ua: str, key: str) -> RelayTarget:
        try:
            pick = await self._pick_code(file_id)
        except RelayError:
            raise
        except SourceChangedError:
            raise
        except Exception as exc:
            raise RelayError(502, f"115 lookup failed: {type(exc).__name__}") from exc
        if self.cache_key(file_id, ua) != key:
            raise SourceChangedError("pan source changed during STRM lookup")
        client = self._pan.get_client()
        target: RelayTarget | None = None
        try:
            url = await self._pan.read_after_check(
                lambda: client.download_url(pick, user_agent=ua)
            )
            if url:
                target = RelayTarget(url=url, source="download")
        except SourceChangedError:
            raise
        except Exception as exc:
            logger.info("115 downurl failed (%s); trying play URL", type(exc).__name__)
        if target is None:
            try:
                url = await self._pan.read_after_check(
                    lambda: client.video_play_url(pick, user_agent=ua)
                )
                if url:
                    target = RelayTarget(url=url, source="play")
            except SourceChangedError:
                raise
            except Exception as exc:
                raise RelayError(502, f"115 play URL failed: {type(exc).__name__}") from exc
        if target is None:
            raise RelayError(502, "115 returned no playable URL")
        wall_expires = stream_cache_expiry(target.url)
        if wall_expires is not None:
            self._remember_url(key, target, wall_expires)
        # Unknown expiry: return but do not cache (players may still use it once).
        return target
