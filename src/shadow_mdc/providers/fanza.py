import asyncio
import re
from urllib.parse import quote

import httpx
from pydantic import BaseModel, ConfigDict
from selectolax.parser import HTMLParser

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError
from .html import first_text, parse_date
from .html_fields import field_links, field_text, first_image_artwork, integer_minutes, sample_image_artwork
from .ratings import parse_hreview_rating, parse_short_reviews

_FANZA_GRAPHQL_URL = "https://api.video.dmm.co.jp/graphql"
_FANZA_RANKING_QUERY = """
query ContentRankingPage($limit: Int!, $offset: Int!, $filter: PPVContentRankingFilterInput) {
  ppvContentRanking(limit: $limit, offset: $offset, filter: $filter) {
    items {
      id
      rank
      content {
        title
        packageImage { mediumUrl largeUrl }
        actresses { id name }
      }
    }
  }
}
"""
_FANZA_LATEST_QUERY = """
query NewReleaseRankingPage($limit: Int!) {
  legacySearchPPV(
    limit: $limit
    floor: AV
    filter: {legacyReleaseStatus: LATEST_RELEASE}
    sort: SALES_RANK_SCORE
  ) {
    result {
      contents {
        id
        title
        actresses { id name }
        packageImage { largeUrl mediumUrl }
      }
    }
  }
}
"""
_FANZA_DETAIL_QUERY = """
query FanzaContentDetail($id: ID!) {
  ppvContent(id: $id) {
    id
    title
    description(format: PLAIN)
    packageImage { largeUrl mediumUrl }
    actresses { id name }
    maker { id name }
    label { id name }
    series { id name }
    genres { id name }
    directors { id name }
    deliveryStartDate
  }
}
"""

_RANKING_PERIODS: dict[str, str] = {
    "rankings_daily": "daily",
    "rankings_weekly": "weekly",
    "rankings_monthly": "monthly",
}

# VR-dedicated list names (prefer weekly for the weekly-VR job). GraphQL may reject
# floor=VR; fetch_ranking then falls back to AV ranking filtered to VR titles.
_VR_RANKING_LISTS: dict[str, str] = {
    "rankings_daily_vr": "rankings_daily",
    "rankings_weekly_vr": "rankings_weekly",
    "rankings_monthly_vr": "rankings_monthly",
}

_RANKING_FILTERS: dict[str, dict[str, dict[str, str]]] = {
    "rankings_daily": {"daily": {"floor": "AV"}},
    "rankings_weekly": {"weekly": {"floor": "AV"}},
    "rankings_monthly": {"monthly": {"floor": "AV"}},
}

# Common FANZA VR label prefixes seen in cid / codes (SIVR-171, 13dsvr01760, …).
_VR_CID_HINT = re.compile(
    r"(?i)(?:^|[^a-z])(?:\d{0,5})?(?:"
    r"sivr|ipvr|mdvr|kavr|savr|vrkm|bibivr|ajvr|dsvr|urvrsp|crvr|juvr|pxvr|"
    r"fcvr|predvr|hnvr|wavr|pppdvr|ebvr|atvr|mvvr|dtvr|prvr|kmvr|bikmvr"
    r")\d"
)


class FanzaRankingItem(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    content_id: str
    rank: int
    title: str
    code: str | None = None
    thumb_url: str | None = None
    actresses: tuple[str, ...] = ()
    source_url: str


class FanzaProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def base_url(self) -> str:
        return self._base_url

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="fanza",
            name="FANZA / DMM",
            query_modes=frozenset({QueryMode.CODE, QueryMode.URL, QueryMode.EXTERNAL_ID}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        content_ids: tuple[str, ...] = ()
        requested_code: str | None = hints.code
        if hints.mode is QueryMode.URL and hints.source_url:
            match = re.search(r"(?i)(?:cid=|/content/\?id=)([a-z0-9_]+)", hints.source_url)
            if match:
                content_ids = (match.group(1).casefold(),)
                requested_code = content_id_to_code(match.group(1)) or requested_code
        elif hints.mode is QueryMode.EXTERNAL_ID:
            external = (hints.external_ids or {}).get("fanza") or hints.term
            if external:
                content_ids = (external.casefold(),)
                requested_code = content_id_to_code(external) or requested_code
        if not content_ids:
            requested = requested_code or hints.term
            parsed, family = extract_code(requested)
            if parsed is None or family is not ContentFamily.JAV or parsed.startswith("FC2-"):
                return []
            requested_code = parsed
            content_ids = _content_id_candidates(parsed)
        if requested_code is None and content_ids:
            requested_code = content_id_to_code(content_ids[0])

        for content_id in content_ids:
            url = f"{self._base_url}/digital/videoa/-/detail/=/cid={quote(content_id)}/"
            record: ProviderRecord | None = None
            try:
                record = await self._detail_graphql(content_id, fallback_code=requested_code)
            except ProviderError as exc:
                if exc.reason == "http" and "status=404" in exc.detail:
                    continue
                # HTML fallback for hosts where classic detail pages still work.
            try:
                html = await self._get_text(
                    self.descriptor.id,
                    url,
                    headers={
                        "Accept-Language": "ja,en-US;q=0.9",
                        "Cookie": "age_check_done=1",
                    },
                )
            except ProviderError as exc:
                if exc.reason == "http" and "status=404" in exc.detail:
                    if record is not None:
                        return [record]
                    continue
                if record is not None:
                    return [record]
                raise
            lowered = html.casefold()
            if "not available in your region" in lowered or "/login/" in lowered:
                if record is not None:
                    return [record]
                raise ProviderError(self.descriptor.id, "blocked", "region or login restriction")
            root = HTMLParser(html)
            if root.css_first(".hreview") is None:
                if record is not None:
                    return [record]
                continue
            html_record = self._parse(root, url, requested_code or content_id, content_id)
            if record is None:
                return [html_record]
            # Merge provider sample stills + ratings from HTML onto GraphQL base.
            return [_merge_fanza_enrichment(record, html_record)]
        return []

    async def fetch_ranking(
        self,
        list_name: str,
        *,
        limit: int = 100,
        offset: int = 0,
        floor: str | None = None,
    ) -> list[FanzaRankingItem]:
        """Fetch FANZA/DMM ranking (日/週/月) or latest-sales list via public GraphQL.

        ``floor`` defaults to ``AV``. Pass ``VR`` (or use ``rankings_*_vr`` list names)
        for VR popularity. When the API rejects ``floor=VR`` (current GraphQL enum),
        fall back to the AV ranking filtered to VR titles/cids.
        """

        vr_requested = False
        logical = list_name
        if list_name in _VR_RANKING_LISTS:
            logical = _VR_RANKING_LISTS[list_name]
            vr_requested = True
        requested_floor = (floor or ("VR" if vr_requested else "AV")).strip().upper() or "AV"
        if requested_floor == "VR":
            vr_requested = True

        if logical == "latest":
            payload: dict[str, object] = {
                "operationName": "NewReleaseRankingPage",
                "query": _FANZA_LATEST_QUERY,
                "variables": {"limit": max(1, limit)},
            }
            raw = await self._graphql(payload, referer_term="daily")
            contents = (
                ((raw.get("data") or {}).get("legacySearchPPV") or {}).get("result") or {}
            ).get("contents")
            if not isinstance(contents, list):
                return []
            items: list[FanzaRankingItem] = []
            for index, row in enumerate(contents, start=1):
                parsed = _ranking_from_content(
                    row if isinstance(row, dict) else {},
                    rank=index,
                    base_url=self._base_url,
                )
                if parsed is None:
                    continue
                if vr_requested and not is_fanza_vr_item(parsed):
                    continue
                items.append(parsed)
                if len(items) >= limit:
                    break
            return _renumber_ranks(items)

        if logical not in _RANKING_PERIODS:
            raise ProviderError(self.descriptor.id, "unsupported", f"unknown ranking list: {list_name}")

        period = _RANKING_PERIODS[logical]
        referer_term = period
        fetch_limit = max(1, limit)
        fetch_offset = max(0, offset)
        # When filtering AV→VR, pull a wider page so ~10 VR titles remain.
        if vr_requested and requested_floor == "VR":
            fetch_limit = max(fetch_limit, min(100, limit * 8))
            fetch_offset = 0

        try:
            items = await self._fetch_content_ranking(
                period=period,
                floor=requested_floor,
                limit=fetch_limit,
                offset=fetch_offset,
                referer_term=referer_term,
            )
        except ProviderError:
            # GraphQL currently rejects floor=VR (HTTP 422 / enum validation).
            # Soft-fallback: AV ranking filtered to VR titles/cids.
            if not (vr_requested and requested_floor == "VR"):
                raise
            items = await self._fetch_content_ranking(
                period=period,
                floor="AV",
                limit=fetch_limit,
                offset=fetch_offset,
                referer_term=referer_term,
            )

        if vr_requested:
            items = [item for item in items if is_fanza_vr_item(item)]
        return _renumber_ranks(items[: max(1, limit)])

    async def _fetch_content_ranking(
        self,
        *,
        period: str,
        floor: str,
        limit: int,
        offset: int,
        referer_term: str,
    ) -> list[FanzaRankingItem]:
        payload: dict[str, object] = {
            "operationName": "ContentRankingPage",
            "query": _FANZA_RANKING_QUERY,
            "variables": {
                "limit": max(1, limit),
                "offset": max(0, offset),
                "filter": {period: {"floor": floor}},
            },
        }
        raw = await self._graphql(payload, referer_term=referer_term)
        ranking = (raw.get("data") or {}).get("ppvContentRanking") or {}
        rows = ranking.get("items") if isinstance(ranking, dict) else None
        if not isinstance(rows, list):
            return []
        items: list[FanzaRankingItem] = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            content = row.get("content") if isinstance(row.get("content"), dict) else {}
            rank = int(row.get("rank") or 0) or (len(items) + 1)
            merged = dict(content)
            if row.get("id") and not merged.get("id"):
                merged["id"] = row["id"]
            parsed = _ranking_from_content(merged, rank=rank, base_url=self._base_url)
            if parsed is not None:
                items.append(parsed)
        return items

    async def _detail_graphql(self, content_id: str, *, fallback_code: str | None) -> ProviderRecord:
        raw = await self._graphql(
            {
                "operationName": "FanzaContentDetail",
                "query": _FANZA_DETAIL_QUERY,
                "variables": {"id": content_id},
            }
        )
        content = ((raw.get("data") or {}).get("ppvContent") or {})
        if not isinstance(content, dict) or not content.get("id"):
            raise ProviderError(self.descriptor.id, "parse", f"content missing for {content_id}")
        title = str(content.get("title") or "").strip()
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        code = content_id_to_code(str(content.get("id"))) or fallback_code
        parsed, family = extract_code(code or "")
        if parsed is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        package = content.get("packageImage") if isinstance(content.get("packageImage"), dict) else {}
        thumb = package.get("largeUrl") or package.get("mediumUrl")
        artwork_list: list[Artwork] = []
        if thumb:
            artwork_list.append(Artwork(url=str(thumb), kind="poster"))
        raw_samples = content.get("sampleImages")
        if isinstance(raw_samples, list):
            for sample in raw_samples:
                if not isinstance(sample, dict):
                    continue
                sample_url = sample.get("largeUrl") or sample.get("mediumUrl")
                if sample_url:
                    artwork_list.append(Artwork(url=str(sample_url), kind="sample"))
        artwork = tuple(artwork_list)
        actresses = tuple(
            str(a.get("name")).strip()
            for a in (content.get("actresses") or [])
            if isinstance(a, dict) and str(a.get("name") or "").strip()
        )
        tags = tuple(
            str(g.get("name")).strip()
            for g in (content.get("genres") or [])
            if isinstance(g, dict) and str(g.get("name") or "").strip()
        )
        directors = tuple(
            str(d.get("name")).strip()
            for d in (content.get("directors") or [])
            if isinstance(d, dict) and str(d.get("name") or "").strip()
        )
        maker = content.get("maker") if isinstance(content.get("maker"), dict) else {}
        label = content.get("label") if isinstance(content.get("label"), dict) else {}
        series = content.get("series") if isinstance(content.get("series"), dict) else {}
        source_url = f"{self._base_url}/digital/videoa/-/detail/=/cid={quote(content_id)}/"
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=content_id,
            source_url=source_url,
            code=parsed,
            title=title,
            original_title=title,
            family=ContentFamily.JAV,
            release_date=parse_date(str(content.get("deliveryStartDate") or "")),
            studio=str(maker.get("name")).strip() if maker.get("name") else None,
            label=str(label.get("name")).strip() if label.get("name") else None,
            series=str(series.get("name")).strip() if series.get("name") else None,
            plot=str(content.get("description") or "").strip() or None,
            actors=actresses,
            directors=directors,
            tags=tags,
            artwork=artwork,
            language="ja",
        )

    async def _graphql(
        self,
        payload: dict[str, object],
        *,
        referer_term: str = "daily",
    ) -> dict[str, object]:
        term = referer_term if referer_term in {"daily", "weekly", "monthly"} else "daily"
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Origin": "https://video.dmm.co.jp",
            "Referer": f"https://video.dmm.co.jp/av/ranking/?term={term}",
            "Cookie": "age_check_done=1",
        }
        response = None
        for attempt in range(self._retries + 1):
            try:
                response = await self._client.post(
                    _FANZA_GRAPHQL_URL,
                    json=payload,
                    headers=headers,
                )
                response.raise_for_status()
                break
            except httpx.TimeoutException as exc:
                if attempt < self._retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                reason = "connect_timeout" if isinstance(exc, httpx.ConnectTimeout) else "timeout"
                raise ProviderError(self.descriptor.id, reason, str(exc)) from exc
            except httpx.HTTPStatusError as exc:
                code = exc.response.status_code
                if attempt < self._retries and (code == 429 or code >= 500):
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                detail = f"status={code}"
                # Surface GraphQL validation messages (e.g. invalid PPVFloor=VR → 422).
                try:
                    body = exc.response.json()
                except ValueError:
                    body = None
                if isinstance(body, dict) and body.get("errors"):
                    detail = f"status={code}; {str(body.get('errors'))[:400]}"
                raise ProviderError(
                    self.descriptor.id,
                    "blocked" if code in {403, 429} else "http",
                    detail,
                ) from exc
            except httpx.HTTPError as exc:
                if attempt < self._retries:
                    await asyncio.sleep(0.25 * (attempt + 1))
                    continue
                raise ProviderError(self.descriptor.id, "network", str(exc)) from exc
        if response is None:
            raise ProviderError(self.descriptor.id, "network", "graphql request produced no response")
        try:
            raw = response.json()
        except ValueError as exc:
            raise ProviderError(self.descriptor.id, "parse", "graphql response is not JSON") from exc
        if not isinstance(raw, dict):
            raise ProviderError(self.descriptor.id, "parse", "graphql response is not an object")
        errors = raw.get("errors")
        if errors:
            raise ProviderError(self.descriptor.id, "api", str(errors)[:500])
        return {str(k): v for k, v in raw.items()}

    def _parse(
        self,
        root: HTMLParser,
        url: str,
        requested: str,
        content_id: str,
    ) -> ProviderRecord:
        title = first_text(root, (".hreview h1", "h1#title", "h1"))
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        raw_code = field_text(root, ("品番",)) or requested
        code, family = extract_code(raw_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        runtime = integer_minutes(field_text(root, ("収録時間",)))
        plot = first_text(root, (".mg-b20.lh4", ".mg-b20.lh4 p", ".product-description"))
        actors = tuple(dict.fromkeys((*field_links(root, ("出演者",)), *tuple(_texts(root, "#performer a")))))
        cover = first_image_artwork(
            root,
            self._base_url,
            ("#sample-video a", "img[name=package-image]"),
        )
        samples = sample_image_artwork(
            root,
            self._base_url,
            (
                "#sample-image-block a img",
                "#sample-image-block a",
                "#sample-image a img",
                "#sample-image a",
                ".sample-image-block a img",
                ".sample-image-block a",
                "a[name^=package-src-] img",
                "a[name^=package-src-]",
            ),
            limit=12,
        )
        rating_value, rating_count, rating_max = parse_hreview_rating(root)
        reviews = parse_short_reviews(root, provider=self.descriptor.id)
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=content_id,
            source_url=url,
            code=code,
            title=title,
            original_title=title,
            family=ContentFamily.JAV,
            release_date=parse_date(field_text(root, ("配信開始日", "発売日")) or ""),
            runtime_seconds=runtime * 60 if runtime is not None else None,
            studio=next(iter(field_links(root, ("メーカー",))), None),
            label=next(iter(field_links(root, ("レーベル",))), None),
            series=next(iter(field_links(root, ("シリーズ",))), None),
            plot=plot,
            actors=actors,
            directors=field_links(root, ("監督",)),
            tags=field_links(root, ("ジャンル",)),
            artwork=tuple((*cover, *samples)),
            language="ja",
            rating=rating_value,
            rating_max=rating_max,
            rating_count=rating_count,
            reviews=reviews,
        )



def is_fanza_vr_item(item: FanzaRankingItem) -> bool:
    """True when ranking row looks like a FANZA VR title (title marker or VR cid/code)."""

    title = (item.title or "").strip()
    # Prefer explicit FANZA VR title marker; avoid matching mid-title "VR" words.
    if "【VR】" in title or re.match(r"(?i)^(?:【)?vr(?:】|\b)", title):
        return True
    cid = (item.content_id or "").strip()
    if cid and _VR_CID_HINT.search(cid):
        return True
    code = (item.code or "").strip().upper()
    return bool(
        code
        and re.match(
            r"^(?:\d{1,5})?(?:SIVR|IPVR|MDVR|KAVR|SAVR|VRKM|BIBIVR|AJVR|DSVR|URVRSP|CRVR|"
            r"JUVR|PXVR|FCVR|PREDVR|HNVR|WAVR|PPPDVR|EBVR|ATVR|MVVR|DTVR|PRVR|KMVR|BIKMVR)-",
            code,
        )
    )


def _renumber_ranks(items: list[FanzaRankingItem]) -> list[FanzaRankingItem]:
    return [
        item.model_copy(update={"rank": index}) if item.rank != index else item
        for index, item in enumerate(items, start=1)
    ]


def content_id_to_code(content_id: str) -> str | None:
    """Map FANZA cid (e.g. mida00726) to catalog code (MIDA-726)."""

    raw = content_id.strip()
    if not raw:
        return None
    match = re.fullmatch(r"(?i)(?:h_\d+)?([a-z]{2,10})(\d{2,6})", raw)
    if match is None:
        match = re.fullmatch(r"(?i)((?:\d{2,5})?[a-z]{2,10})(\d{2,6})", raw)
    if match is not None:
        prefix, digits = match.groups()
        candidate = f"{prefix.upper()}-{int(digits)}"
        parsed, _family = extract_code(candidate)
        if parsed is not None:
            return parsed
    parsed, _family = extract_code(raw)
    return parsed


def _ranking_from_content(
    content: dict,
    *,
    rank: int,
    base_url: str,
) -> FanzaRankingItem | None:
    content_id = str(content.get("id") or "").strip()
    title = str(content.get("title") or "").strip()
    if not content_id or not title:
        return None
    package = content.get("packageImage") if isinstance(content.get("packageImage"), dict) else {}
    thumb = package.get("largeUrl") or package.get("mediumUrl")
    actresses = tuple(
        str(a.get("name")).strip()
        for a in (content.get("actresses") or [])
        if isinstance(a, dict) and a.get("name") and str(a.get("name")).strip()
    )
    source_url = f"{base_url.rstrip('/')}/digital/videoa/-/detail/=/cid={content_id}/"
    return FanzaRankingItem(
        content_id=content_id,
        rank=rank,
        title=title,
        code=content_id_to_code(content_id),
        thumb_url=str(thumb) if thumb else None,
        actresses=actresses,
        source_url=source_url,
    )


def _content_id_candidates(code: str) -> tuple[str, ...]:
    match = re.fullmatch(r"(?i)([A-Z0-9]+)-(\d+)", code.strip())
    if match is None:
        return (code.replace("-", "").casefold(),)
    prefix, digits = match.groups()
    values = (
        f"{prefix.casefold()}{digits.zfill(5)}",
        f"{prefix.casefold()}{digits}",
        code.replace("-", "").casefold(),
    )
    return tuple(dict.fromkeys(values))


def _texts(root: HTMLParser, selector: str) -> list[str]:
    return [value for node in root.css(selector) if (value := node.text(separator=" ", strip=True))]
