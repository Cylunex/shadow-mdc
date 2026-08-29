import json
import re
from collections.abc import Iterable
from datetime import date

from selectolax.parser import HTMLParser

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from .base import HttpProvider, ProviderError


class JsonLdProvider(HttpProvider):
    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="jsonld",
            name="Generic JSON-LD",
            query_modes=frozenset({QueryMode.URL}),
            families=frozenset(ContentFamily),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        if hints.source_url is None:
            return []
        html = await self._get_text(self.descriptor.id, hints.source_url)
        root = HTMLParser(html)
        documents: list[dict[str, object]] = []
        for node in root.css('script[type="application/ld+json"]'):
            try:
                value = json.loads(node.text())
            except json.JSONDecodeError:
                continue
            documents.extend(_objects(value))
        candidates = [
            item
            for item in documents
            if str(item.get("@type", "")).casefold() in {"movie", "videoobject", "creativework", "episode"}
        ]
        if not candidates:
            raise ProviderError(self.descriptor.id, "parse", "no supported JSON-LD object")
        return [self._record(item, hints.source_url) for item in candidates[:5]]

    def _record(self, item: dict[str, object], source_url: str) -> ProviderRecord:
        title = _string(item.get("name")) or _string(item.get("headline"))
        if not title:
            raise ProviderError(self.descriptor.id, "parse", "JSON-LD object has no title")
        canonical_url = _string(item.get("url")) or source_url
        actors = tuple(_names(item.get("actor") or item.get("actors")))
        directors = tuple(_names(item.get("director")))
        tags = tuple(
            value.strip() for value in (_string(item.get("keywords")) or "").split(",") if value.strip()
        )
        artwork = tuple(
            Artwork.model_validate({"url": url, "kind": "thumb"})
            for url in _image_urls(item.get("image"))
            if url.startswith(("http://", "https://"))
        )
        runtime = _iso_duration_seconds(_string(item.get("duration")))
        released = _date(_string(item.get("datePublished")) or _string(item.get("uploadDate")))
        external_id = _string(item.get("@id")) or canonical_url
        studio = _first_name(item.get("productionCompany") or item.get("publisher"))
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=external_id,
            source_url=canonical_url,
            title=title,
            release_date=released,
            runtime_seconds=runtime,
            studio=studio,
            plot=_string(item.get("description")),
            actors=actors,
            directors=directors,
            tags=tags,
            artwork=artwork,
        )


def _objects(value: object) -> list[dict[str, object]]:
    if isinstance(value, list):
        output: list[dict[str, object]] = []
        for item in value:
            output.extend(_objects(item))
        return output
    if not isinstance(value, dict):
        return []
    typed = {str(key): item for key, item in value.items()}
    graph = typed.get("@graph")
    return [typed, *_objects(graph)] if graph is not None else [typed]


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _names(value: object) -> Iterable[str]:
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str) and item.strip():
            yield item.strip()
        elif isinstance(item, dict):
            name = _string(item.get("name"))
            if name:
                yield name


def _first_name(value: object) -> str | None:
    return next(iter(_names(value)), None)


def _image_urls(value: object) -> Iterable[str]:
    items = value if isinstance(value, list) else [value]
    for item in items:
        if isinstance(item, str):
            yield item
        elif isinstance(item, dict):
            url = _string(item.get("url") or item.get("contentUrl"))
            if url:
                yield url


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None


def _iso_duration_seconds(value: str | None) -> int | None:
    if value is None or not value.startswith("PT"):
        return None
    match = re.fullmatch(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?", value)
    if match is None:
        return None
    hours, minutes, seconds = (int(part or 0) for part in match.groups())
    return hours * 3600 + minutes * 60 + seconds
