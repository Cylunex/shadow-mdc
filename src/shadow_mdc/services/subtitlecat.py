"""Chinese subtitles from SubtitleCat (subtitlecat.com), searched by 番号.

Search by catalogue number and check the subtitle's edition before reading its
detail page and downloading its Chinese SRT.

Best-effort only: every failure is logged and returned as a result, never raised,
so a scrape / STRM export never fails because of subtitles. Misses are cached on
disk for ``miss_ttl_hours`` and requests are spaced by ``min_interval_seconds``.
"""

from __future__ import annotations

import html
import json
import logging
import os
import re
import threading
import time
import unicodedata
import uuid
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urljoin

import httpx

from ..identity import extract_code
from ..media.nfo_import import parse_movie_nfo, resolve_sidecar_nfo
from ..media.parts import (
    SUBTITLE_EXTENSIONS,
    media_stem_is_ambiguous,
    subtitle_matches_stem,
    subtitle_satisfies_chinese_backfill,
)
from ..media.subtitle_validation import SrtTiming, inspect_srt
from ..normalize_code import is_format_equivalent
from ..remaster import parse_remaster_identity, strip_remaster_marker
from .strm_export import read_sidecar

logger = logging.getLogger(__name__)

ZH_LANGS: tuple[str, ...] = ("zh-CN", "zh-TW")
SUBTITLE_SUFFIX = ".zh.srt"  # Emby/Kodi: <name>.zh.srt beside <name>.strm / video
_ROW = re.compile(
    r'<tr>\s*<td><a href="(?P<href>[^"]+)">(?P<name>[^<]+)</a>(?P<note>[^<]*)</td>(?P<rest>.*?)</tr>',
    re.S,
)
_DOWNLOADS = re.compile(r"metric-value\">\s*([\d,]+)<span[^>]*>\s*downloads", re.I)
_LANG_LINK = re.compile(r'id="download_(?P<lang>zh-(?:CN|TW))"[^>]*?href="(?P<href>[^"]+\.srt)"')
_NON_ALNUM = re.compile(r"[^0-9a-z]+")
_CODE_TOKENS = re.compile(r"[a-z]+|[0-9]+")
_UNCENSORED_CUT = re.compile(
    r"(?i)(?<![a-z0-9])(?:uncensored|u|uc)(?![a-z0-9])|无码|無碼|去码|去碼"
)
_LEAKED_CUT = re.compile(r"(?i)(?<![a-z0-9])leaked(?![a-z0-9])|流出")
_EXTENDED_CUT = re.compile(r"(?i)(?<![a-z0-9])extended(?![a-z0-9])|加长版|加長版")
_REMASTERED_CUT = re.compile(r"(?i)(?<![a-z0-9])remaster(?:ed)?(?![a-z0-9])|重制版|重製版")
_GLUED_REMASTER = re.compile(r"(?i)\d[ez]?(?:hd|ai|h)$")


def _compact(value: str) -> str:
    return _NON_ALNUM.sub("", value.casefold())


def _code_in_name(name: str, code: str) -> bool:
    found, _ = extract_code(name)
    if found is not None:
        return is_format_equivalent(found, code)
    # Some short serials are outside the general filename parser's range.
    tokens = _CODE_TOKENS.findall(code.casefold())
    if not tokens:
        return False
    pattern = r"[-_. ]*".join(f"0*{int(token)}" if token.isdigit() else token for token in tokens)
    return bool(re.search(rf"(?<![a-z0-9]){pattern}(?![a-z0-9])", name, re.I))


def _edition(name: str) -> frozenset[str]:
    normalized = unicodedata.normalize("NFKC", name)
    labels = frozenset(
        label for label, pattern in (
            ("uncensored", _UNCENSORED_CUT), ("leaked", _LEAKED_CUT),
            ("extended", _EXTENDED_CUT), ("remastered", _REMASTERED_CUT),
        ) if pattern.search(normalized)
    )
    if _GLUED_REMASTER.search(normalized) and parse_remaster_identity(normalized):
        return labels | {"remastered"}
    return labels


def _release_edition(directory: Path, stem: str) -> frozenset[str]:
    for entry in read_sidecar(directory):
        if Path(entry.name).stem == stem and entry.remote_path:
            return _edition(PurePosixPath(entry.remote_path).stem) or _edition(stem)
    return _edition(stem)


def _timing_warning(directory: Path, stem: str, timing: SrtTiming) -> str | None:
    try:
        # A full-work NFO duration cannot be compared with individual parts.
        streams = [p for p in directory.iterdir() if p.suffix.casefold() == ".strm" and p.is_file()]
        if len(streams) != 1 or streams[0].stem != stem:
            return None
        nfo = resolve_sidecar_nfo(streams[0])
        runtime = parse_movie_nfo(nfo).runtime_seconds if nfo is not None else None
    except (OSError, ValueError):
        return None
    if runtime and (
        timing.end_seconds > runtime + 120
        or timing.end_seconds < runtime - max(180, runtime * 0.25)
    ):
        return f"subtitle ends at {timing.end_seconds:g}s, NFO runtime {runtime}s ({timing.cues} cues)"
    return None


@dataclass(frozen=True, slots=True)
class SubtitleCatHit:
    name: str
    detail_url: str
    downloads: int
    from_chinese: bool


@dataclass(frozen=True, slots=True)
class SubtitleCatPick:
    code: str
    lang: str
    url: str
    detail_url: str
    name: str


@dataclass(slots=True)
class SubtitleFetchResult:
    code: str
    status: str  # written | exists | ambiguous | miss | cached-miss | error | dry-run | disabled
    path: Path | None = None
    pick: SubtitleCatPick | None = None
    detail: str | None = None


def parse_search(
    page: str, code: str, base_url: str, *, edition: frozenset[str] = frozenset(),
) -> list[SubtitleCatHit]:
    """Rows with a matching catalogue number and edition, best first."""
    hits: list[SubtitleCatHit] = []
    for match in _ROW.finditer(page):
        name = html.unescape(match.group("name")).strip()
        normalized = unicodedata.normalize("NFKC", name)
        suffix = Path(normalized).suffix
        stem = normalized.removesuffix(suffix) if suffix.casefold() in SUBTITLE_EXTENSIONS else normalized
        if not _code_in_name(stem, code) or _edition(stem) != edition:
            continue
        downloads_match = _DOWNLOADS.search(match.group("rest"))
        downloads = int(downloads_match.group(1).replace(",", "")) if downloads_match else 0
        note = match.group("note").casefold()
        from_chinese = "from chinese" in note or bool(re.search(r"(zh|chs|cht|中文|简|繁)", name, re.I))
        hits.append(
            SubtitleCatHit(
                name=name,
                detail_url=urljoin(base_url.rstrip("/") + "/", match.group("href")),
                downloads=downloads,
                from_chinese=from_chinese,
            )
        )
    # Native Chinese sources first (machine-translated JP→ZH is the fallback), then popularity.
    hits.sort(key=lambda hit: (not hit.from_chinese, -hit.downloads))
    return hits


def parse_detail_zh_links(page: str, base_url: str) -> dict[str, str]:
    links: dict[str, str] = {}
    for match in _LANG_LINK.finditer(page):
        links.setdefault(
            match.group("lang"), urljoin(base_url.rstrip("/") + "/", html.unescape(match.group("href")))
        )
    return links


def has_existing_subtitle(directory: Path, stem: str) -> bool:
    """Return whether there is any matching subtitle, regardless of language."""
    try:
        return any(
            entry.suffix.casefold() in SUBTITLE_EXTENSIONS
            and (entry.is_symlink() or entry.is_file())
            and subtitle_matches_stem(entry, stem)
            for entry in directory.iterdir()
        )
    except OSError:
        return False


def chinese_subtitle_state(directory: Path, stem: str) -> Literal["exists", "ambiguous"] | None:
    try:
        siblings = list(directory.iterdir())
    except FileNotFoundError:
        return None
    if media_stem_is_ambiguous(siblings, stem):
        return "ambiguous"
    for entry in siblings:
        if (entry.suffix.casefold() in SUBTITLE_EXTENSIONS and (entry.is_symlink() or entry.is_file())
                and subtitle_satisfies_chinese_backfill(entry, stem, siblings)):
            return "exists"
    # The destination may itself be another video's exact stem (Film.zh.strm).
    if not subtitle_satisfies_chinese_backfill(directory / f"{stem}{SUBTITLE_SUFFIX}", stem, siblings):
        return "ambiguous"
    return None


@dataclass
class SubtitleCatFetcher:
    base_url: str = "https://www.subtitlecat.com"
    timeout_seconds: float = 20
    min_interval_seconds: float = 3.0
    miss_ttl_hours: float = 72
    cache_path: Path | None = None
    proxy_url: str | None = None
    user_agent: str = "Mozilla/5.0 (compatible; ShadowMDC)"
    max_detail_pages: int = 3
    client: httpx.Client | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    _last_request: float = field(default=0.0, repr=False)

    # -- cache -----------------------------------------------------------------
    def _load_cache(self) -> dict[str, float]:
        if self.cache_path is None or not self.cache_path.is_file():
            return {}
        try:
            payload = json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}
        return {str(k): float(v) for k, v in payload.items()} if isinstance(payload, dict) else {}

    def _miss_cached(self, key: str) -> bool:
        stamp = self._load_cache().get(key)
        return stamp is not None and time.time() - stamp < self.miss_ttl_hours * 3600

    def _remember_miss(self, key: str) -> None:
        if self.cache_path is None:
            return
        cache = self._load_cache()
        cutoff = time.time() - self.miss_ttl_hours * 3600
        cache = {k: v for k, v in cache.items() if v >= cutoff}
        cache[key] = time.time()
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.cache_path.with_suffix(".tmp")
            tmp.write_text(json.dumps(cache), encoding="utf-8")
            tmp.replace(self.cache_path)
        except OSError as exc:
            logger.debug("subtitlecat cache write failed: %s", exc)

    # -- http ------------------------------------------------------------------
    def _get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        with self._lock:
            wait = self.min_interval_seconds - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
        client = self.client
        if client is None:
            client = httpx.Client(
                timeout=self.timeout_seconds,
                proxy=self.proxy_url or None,
                follow_redirects=True,
                headers={"User-Agent": self.user_agent},
            )
            self.client = client
        response = client.get(url, params=params, timeout=self.timeout_seconds)
        response.raise_for_status()
        return response

    def lookup(self, code: str, *, edition: frozenset[str] | None = None) -> SubtitleCatPick | None:
        """Best zh-CN (else zh-TW) subtitle for ``code``; None when not found."""

        base_code = strip_remaster_marker(code)
        if edition is None:
            edition = frozenset({"remastered"}) if base_code != code.strip() else frozenset()
        page = self._get(f"{self.base_url.rstrip('/')}/index.php", params={"search": base_code}).text
        hits = parse_search(page, base_code, self.base_url, edition=edition)
        fallback: SubtitleCatPick | None = None
        for hit in hits[: self.max_detail_pages]:
            links = parse_detail_zh_links(self._get(hit.detail_url).text, self.base_url)
            for lang in ZH_LANGS:
                if lang in links:
                    pick = SubtitleCatPick(
                        code=code, lang=lang, url=links[lang], detail_url=hit.detail_url, name=hit.name
                    )
                    if lang == "zh-CN":
                        return pick
                    fallback = fallback or pick
        return fallback

    def fetch(self, code: str, directory: Path, stem: str, *, dry_run: bool = False) -> SubtitleFetchResult:
        """Write ``<directory>/<stem>.zh.srt``; never raises."""

        target = directory / f"{stem}{SUBTITLE_SUFFIX}"
        try:
            local_state = chinese_subtitle_state(directory, stem)
            if local_state:
                return SubtitleFetchResult(code=code, status=local_state, path=target)
            edition = _release_edition(directory, stem)
            if parse_remaster_identity(code):
                edition = edition | {"remastered"}
            cache_key = f"v2:{_compact(code)}:{','.join(sorted(edition))}"
            if not dry_run and self._miss_cached(cache_key):
                return SubtitleFetchResult(code=code, status="cached-miss")
            pick = self.lookup(code, edition=edition)
            if pick is None:
                if not dry_run:
                    self._remember_miss(cache_key)
                return SubtitleFetchResult(code=code, status="miss")
            if dry_run:
                return SubtitleFetchResult(code=code, status="dry-run", path=target, pick=pick)
            body = self._get(pick.url).content
            timing = inspect_srt(body)
            if isinstance(timing, str):
                return SubtitleFetchResult(code=code, status="error", pick=pick, detail=timing)
            warning = _timing_warning(directory, stem, timing)
            if warning:
                logger.warning("subtitlecat %s: %s", code, warning)
            directory.mkdir(parents=True, exist_ok=True)
            # Publish a complete file without replacing a concurrent/user subtitle.
            temporary = target.with_name(f".{target.name}.{uuid.uuid4().hex}.part")
            descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o666)
            try:
                with os.fdopen(descriptor, "wb") as stream:
                    stream.write(body)
                    stream.flush()
                    os.fsync(stream.fileno())
                try:
                    os.link(temporary, target)
                except FileExistsError:
                    return SubtitleFetchResult(code=code, status="exists", path=target, pick=pick)
            finally:
                temporary.unlink(missing_ok=True)
            return SubtitleFetchResult(code=code, status="written", path=target, pick=pick, detail=warning)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("subtitlecat %s failed: %s: %s", code, type(exc).__name__, exc)
            return SubtitleFetchResult(code=code, status="error", detail=f"{type(exc).__name__}: {exc}"[:300])
