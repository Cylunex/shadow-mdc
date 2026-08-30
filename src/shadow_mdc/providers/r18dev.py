import json
from datetime import date
from urllib.parse import quote

import httpx

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, QueryMode
from ..identity import extract_code
from .base import HttpProvider, ProviderError


class R18DevProvider(HttpProvider):
    def __init__(self, client: httpx.AsyncClient, base_url: str, retries: int = 1):
        super().__init__(client, retries)
        self._base_url = base_url.rstrip("/")

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="r18dev",
            name="R18.dev",
            query_modes=frozenset({QueryMode.CODE}),
            families=frozenset({ContentFamily.JAV}),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        requested = hints.code or hints.term
        requested_code, family = extract_code(requested)
        if requested_code is None or family is not ContentFamily.JAV or requested_code.startswith("FC2-"):
            return []
        dvd_id = requested_code.replace("-", "")
        url = f"{self._base_url}/videos/vod/movies/detail/-/dvd_id={quote(dvd_id)}/json"
        try:
            text = await self._get_text(self.descriptor.id, url)
        except ProviderError as exc:
            if exc.reason == "http" and "status=404" in exc.detail:
                return []
            raise
        try:
            payload: object = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ProviderError(self.descriptor.id, "parse", "response is not valid JSON") from exc
        item = _select_item(payload, requested_code)
        if item is None:
            return []
        return [self._record(item, url, requested_code)]

    def _record(self, item: dict[str, object], source_url: str, requested: str) -> ProviderRecord:
        raw_code = _string(item.get("dvd_id")) or requested
        code, family = extract_code(raw_code)
        if code is None or family is not ContentFamily.JAV:
            raise ProviderError(self.descriptor.id, "parse", "valid JAV code missing")
        title_ja = _string(item.get("title_ja"))
        title_en = _string(item.get("title_en"))
        title = title_ja or title_en or _string(item.get("title"))
        if title is None:
            raise ProviderError(self.descriptor.id, "parse", "detail title missing")
        external_id = _string(item.get("content_id")) or code
        artwork = _artwork(item)
        runtime = _integer(item.get("runtime_mins"))
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=external_id,
            source_url=f"{self._base_url}/videos/vod/movies/detail/-/id={external_id}",
            code=code,
            title=title,
            original_title=title_ja or title,
            family=ContentFamily.JAV,
            release_date=_date(_string(item.get("release_date"))),
            runtime_seconds=runtime * 60 if runtime is not None else None,
            studio=_string(item.get("maker_name_ja")) or _string(item.get("maker_name_en")),
            label=_string(item.get("label_name_ja")) or _string(item.get("label_name_en")),
            series=_string(item.get("series_name_ja")) or _string(item.get("series_name_en")),
            plot=_string(item.get("description")),
            actors=_entity_names(item.get("actresses")),
            directors=_entity_names(item.get("directors")),
            tags=_entity_names(item.get("categories")),
            artwork=artwork,
            language="ja" if title_ja else "en",
        )


def _select_item(payload: object, requested: str) -> dict[str, object] | None:
    if isinstance(payload, dict):
        return {str(key): value for key, value in payload.items()} if payload else None
    if not isinstance(payload, list):
        return None
    requested_code, _ = extract_code(requested)
    candidates = [
        {str(key): value for key, value in item.items()} for item in payload if isinstance(item, dict)
    ]
    for item in candidates:
        code, _ = extract_code(_string(item.get("dvd_id")) or "")
        if code == requested_code:
            return item
    return candidates[0] if len(candidates) == 1 else None


def _entity_names(value: object) -> tuple[str, ...]:
    items = value if isinstance(value, list) else []
    names: list[str] = []
    for item in items:
        if isinstance(item, str):
            name = item.strip()
        elif isinstance(item, dict):
            name = next(
                (
                    candidate
                    for key in ("name_kanji", "name_ja", "name_romaji", "name_en", "name")
                    if (candidate := _string(item.get(key)))
                ),
                "",
            )
        else:
            name = ""
        if name and name not in names:
            names.append(name)
    return tuple(names)


def _artwork(item: dict[str, object]) -> tuple[Artwork, ...]:
    urls: list[str] = []
    direct = _string(item.get("jacket_full_url"))
    if direct:
        urls.append(direct)
    images = item.get("images")
    if isinstance(images, dict):
        jacket = images.get("jacket_image")
        if isinstance(jacket, dict):
            urls.extend(value for key in ("large2", "large", "medium") if (value := _string(jacket.get(key))))
    unique = tuple(dict.fromkeys(url for url in urls if url.startswith(("http://", "https://"))))
    return tuple(Artwork.model_validate({"url": url, "kind": "fanart"}) for url in unique[:1])


def _string(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _integer(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _date(value: str | None) -> date | None:
    try:
        return date.fromisoformat(value[:10]) if value else None
    except ValueError:
        return None
