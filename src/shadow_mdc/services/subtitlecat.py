"""Chinese subtitles from SubtitleCat (subtitlecat.com), searched by 番号.

No API key. Flow: ``index.php?search=CODE`` → result rows whose name contains the
compact code → detail page → ``download_zh-CN`` / ``download_zh-TW`` link → ``.srt``.

Best-effort only: every failure is logged and returned as a result, never raised,
so a scrape / STRM export never fails because of subtitles. Misses are cached on
disk for ``miss_ttl_hours`` and requests are spaced by ``min_interval_seconds``.
"""

from __future__ import annotations

import html
import json
import logging
import re
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urljoin

import httpx

from ..media.parts import SUBTITLE_EXTENSIONS

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


def _compact(value: str) -> str:
    return _NON_ALNUM.sub("", value.casefold())


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
    status: str  # written | exists | miss | cached-miss | error | dry-run | disabled
    path: Path | None = None
    pick: SubtitleCatPick | None = None
    detail: str | None = None


def parse_search(page: str, code: str, base_url: str) -> list[SubtitleCatHit]:
    """Rows whose subtitle name contains the compact code, best first."""

    want = _compact(code)
    hits: list[SubtitleCatHit] = []
    for match in _ROW.finditer(page):
        name = html.unescape(match.group("name")).strip()
        compact = _compact(name)
        if not want or want not in compact:
            continue
        # Reject longer codes sharing a prefix (SSIS-0010 vs SSIS-001).
        tail = compact.split(want, 1)[1]
        if tail[:1].isdigit():
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
    folded = stem.casefold()
    try:
        for entry in directory.iterdir():
            if entry.suffix.casefold() in SUBTITLE_EXTENSIONS and entry.name.casefold().startswith(folded):
                return True
    except OSError:
        return False
    return False


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

    def _miss_cached(self, code: str) -> bool:
        stamp = self._load_cache().get(_compact(code))
        return stamp is not None and time.time() - stamp < self.miss_ttl_hours * 3600

    def _remember_miss(self, code: str) -> None:
        if self.cache_path is None:
            return
        cache = self._load_cache()
        cutoff = time.time() - self.miss_ttl_hours * 3600
        cache = {k: v for k, v in cache.items() if v >= cutoff}
        cache[_compact(code)] = time.time()
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

    def lookup(self, code: str) -> SubtitleCatPick | None:
        """Best zh-CN (else zh-TW) subtitle for ``code``; None when not found."""

        page = self._get(f"{self.base_url.rstrip('/')}/index.php", params={"search": code}).text
        hits = parse_search(page, code, self.base_url)
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
            if has_existing_subtitle(directory, stem):
                return SubtitleFetchResult(code=code, status="exists", path=target)
            if not dry_run and self._miss_cached(code):
                return SubtitleFetchResult(code=code, status="cached-miss")
            pick = self.lookup(code)
            if pick is None:
                if not dry_run:
                    self._remember_miss(code)
                return SubtitleFetchResult(code=code, status="miss")
            if dry_run:
                return SubtitleFetchResult(code=code, status="dry-run", path=target, pick=pick)
            body = self._get(pick.url).content
            if len(body) < 32 or b"-->" not in body[:4096]:
                return SubtitleFetchResult(code=code, status="error", pick=pick, detail="not an srt payload")
            directory.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(body)
            tmp.replace(target)
            return SubtitleFetchResult(code=code, status="written", path=target, pick=pick)
        except (httpx.HTTPError, OSError, ValueError) as exc:
            logger.warning("subtitlecat %s failed: %s: %s", code, type(exc).__name__, exc)
            return SubtitleFetchResult(code=code, status="error", detail=f"{type(exc).__name__}: {exc}"[:300])
