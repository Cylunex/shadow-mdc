import re

import httpx
from pydantic import BaseModel, Field, ValidationError

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..normalize_code import normalize_code
from .base import HttpProvider, ProviderError
from .html import parse_date

_PROVIDER = "onepondo"
_SITE_ID = 2470
_CODE = re.compile(r"1PONDO-([0-9]{6})-([0-9]{3})")


class _MovieDetails(BaseModel):
    movie_id: str = Field(alias="MovieID")
    site_id: int = Field(alias="SiteID")
    title: str = Field(alias="Title", min_length=1)
    synopsis: str | None = Field(default=None, alias="Desc")
    released: str | None = Field(default=None, alias="Release")
    duration_seconds: int | None = Field(default=None, alias="Duration", ge=0)
    cast: tuple[str, ...] = Field(default=(), alias="ActressesJa")
    tags: tuple[str, ...] = Field(default=(), alias="UCNAME")
    series: str | None = Field(default=None, alias="Series")
    cover: str | None = Field(default=None, alias="ThumbUltra")
    cover_high: str | None = Field(default=None, alias="ThumbHigh")
    cover_default: str | None = Field(default=None, alias="MovieThumb")


class OnePondoProvider(HttpProvider):
    """First-party metadata for identifiers that explicitly name 1PONDO."""

    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1) -> None:
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id=_PROVIDER,
            name="1PONDO",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        code = normalize_code(hints.code)
        match = _CODE.fullmatch(code)
        if match is None:
            return []
        movie_id = "_".join(match.groups())
        endpoint = f"{self._base_url}/dyn/phpauto/movie_details/movie_id/{movie_id}.json"
        try:
            raw = await self._get_text(_PROVIDER, endpoint)
        except ProviderError as exc:
            if exc.reason == "http" and "status=404;" in exc.detail:
                return []
            raise
        try:
            details = _MovieDetails.model_validate_json(raw)
        except ValidationError as exc:
            fields = ", ".join(str(error["loc"][0]) if error["loc"] else "document" for error in exc.errors())
            raise ProviderError(_PROVIDER, "parse", f"invalid movie details: {fields}") from exc
        if details.movie_id != movie_id or details.site_id != _SITE_ID:
            return []
        title = details.title.strip()
        if not title:
            raise ProviderError(_PROVIDER, "parse", "empty movie title")
        return [
            ProviderRecord(
                provider=_PROVIDER,
                external_id=movie_id,
                source_url=f"{self._base_url}/moviepages/{movie_id}/index.html",
                code=code,
                family=ContentFamily.JAV,
                title=title,
                original_title=title,
                studio="一本道",
                release_date=parse_date(details.released or ""),
                runtime_seconds=details.duration_seconds or None,
                plot=(details.synopsis or "").strip() or None,
                series=(details.series or "").strip() or None,
                actors=details.cast,
                tags=details.tags,
                artwork=_cover_artwork(details),
                language="ja",
            )
        ]


def _cover_artwork(details: _MovieDetails) -> tuple[Artwork, ...]:
    for url in (details.cover, details.cover_high, details.cover_default):
        if not url:
            continue
        try:
            return (Artwork.model_validate({"url": url, "kind": "poster"}),)
        except ValidationError:
            continue
    return ()
