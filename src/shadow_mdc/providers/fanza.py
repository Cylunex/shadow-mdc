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
from .html_fields import field_links, field_text, first_image_artwork, integer_minutes


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

_RANKING_FILTERS: dict[str, dict[str, dict[str, str]]] = {
    "rankings_daily": {"daily": {"floor": "AV"}},
    "rankings_weekly": {"weekly": {"floor": "AV"}},
    "rankings_monthly": {"monthly": {"floor": "AV"}},
}


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
            try:
                return [await self._detail_graphql(content_id, fallback_code=requested_code)]
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
                    continue
                raise
            lowered = html.casefold()
            if "not available in your region" in lowered or "/login/" in lowered:
                raise ProviderError(self.descriptor.id, "blocked", "region or login restriction")
            root = HTMLParser(html)
            if root.css_first(".hreview") is None:
                continue
            return [self._parse(root, url, requested_code or content_id, content_id)]
        return []

    async def fetch_ranking(
        self,
        list_name: str,
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> list[FanzaRankingItem]:
        """Fetch FANZA/DMM AV ranking (日/週/月) or latest-sales list via public GraphQL."""

        if list_name == "latest":
            payload: dict[str, object] = {
                "operationName": "NewReleaseRankingPage",
                "query": _FANZA_LATEST_QUERY,
                "variables": {"limit": max(1, limit)},
            }
        elif list_name in _RANKING_FILTERS:
            payload = {
                "operationName": "ContentRankingPage",
                "query": _FANZA_RANKING_QUERY,
                "variables": {
                    "limit": max(1, limit),
                    "offset": max(0, offset),
                    "filter": _RANKING_FILTERS[list_name],
                },
            }
        else:
            raise ProviderError(self.descriptor.id, "unsupported", f"unknown ranking list: {list_name}")

        raw = await self._graphql(payload)
        if list_name == "latest":
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
                if parsed is not None:
                    items.append(parsed)
            return items

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
        artwork: tuple[Artwork, ...] = ()
        if thumb:
            artwork = (Artwork(url=str(thumb), kind="poster"),)
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

    async def _graphql(self, payload: dict[str, object]) -> dict[str, object]:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Accept-Language": "ja,en-US;q=0.9,en;q=0.8",
            "Origin": "https://video.dmm.co.jp",
            "Referer": "https://video.dmm.co.jp/av/ranking/?term=daily",
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
                raise ProviderError(
                    self.descriptor.id,
                    "blocked" if code in {403, 429} else "http",
                    f"status={code}",
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
            artwork=first_image_artwork(
                root,
                self._base_url,
                ("#sample-video a", "img[name=package-image]"),
            ),
            language="ja",
        )


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
