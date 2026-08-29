from datetime import date

import httpx

from ..domain import Artwork, IdentityHints, ProviderDescriptor, ProviderRecord
from ..enums import ContentFamily, ProviderRequirement, QueryMode
from .base import ProviderError

_FIELDS = """
id title code date duration director details
studio { name }
tags { name }
performers { as performer { name } }
images { url }
fingerprints { hash algorithm }
"""

_SEARCH = f"""
query Search($term: String!) {{
  searchScene(term: $term) {{ {_FIELDS} }}
}}
"""

_FINGERPRINT = f"""
query Find($hash: String!) {{
  findSceneByFingerprint(fingerprint: {{hash: $hash, algorithm: OSHASH}}) {{ {_FIELDS} }}
}}
"""


class ThePornDBProvider:
    def __init__(self, client: httpx.AsyncClient, graphql_url: str, token: str | None):
        self._client = client
        self._graphql_url = graphql_url
        self._token = token

    @property
    def descriptor(self) -> ProviderDescriptor:
        return ProviderDescriptor(
            id="theporndb",
            name="ThePornDB",
            query_modes=frozenset(
                {QueryMode.CODE, QueryMode.TEXT, QueryMode.FINGERPRINT, QueryMode.EXTERNAL_ID}
            ),
            families=frozenset(ContentFamily),
            requirements=frozenset({ProviderRequirement.API_TOKEN}),
            configured=bool(self._token),
        )

    async def search(self, hints: IdentityHints) -> list[ProviderRecord]:
        if self._token is None:
            return []
        if oshash := hints.fingerprints.get("oshash"):
            payload: dict[str, object] = {"query": _FINGERPRINT, "variables": {"hash": oshash}}
            raw = await self._post(payload)
            found = _nested(raw, "data", "findSceneByFingerprint")
            scenes = found if isinstance(found, list) else ([found] if isinstance(found, dict) else [])
            if scenes:
                return [self._record(scene) for scene in scenes if isinstance(scene, dict)]

        payload = {"query": _SEARCH, "variables": {"term": hints.term}}
        raw = await self._post(payload)
        found = _nested(raw, "data", "searchScene")
        if not isinstance(found, list):
            return []
        return [self._record(scene) for scene in found[:10] if isinstance(scene, dict)]

    async def _post(self, payload: dict[str, object]) -> dict[str, object]:
        try:
            response = await self._client.post(
                self._graphql_url,
                json=payload,
                headers={"Authorization": f"Bearer {self._token}"},
            )
            response.raise_for_status()
            raw = response.json()
        except httpx.TimeoutException as exc:
            raise ProviderError(self.descriptor.id, "timeout", str(exc)) from exc
        except httpx.HTTPStatusError as exc:
            raise ProviderError(self.descriptor.id, "http", f"status={exc.response.status_code}") from exc
        except (httpx.HTTPError, ValueError) as exc:
            raise ProviderError(self.descriptor.id, "network", str(exc)) from exc
        if not isinstance(raw, dict):
            raise ProviderError(self.descriptor.id, "parse", "GraphQL response is not an object")
        errors = raw.get("errors")
        if errors:
            raise ProviderError(self.descriptor.id, "api", str(errors)[:500])
        return {str(key): value for key, value in raw.items()}

    def _record(self, scene: dict[object, object]) -> ProviderRecord:
        external_id = _text(scene.get("id"))
        title = _text(scene.get("title"))
        if external_id is None or title is None:
            raise ProviderError(self.descriptor.id, "parse", "scene id/title missing")
        code = _text(scene.get("code"))
        family = ContentFamily.JAV if code else ContentFamily.WESTERN
        studio = _mapping_text(scene.get("studio"), "name")
        tags = tuple(_mapping_text(item, "name") for item in _list(scene.get("tags")))
        actors = tuple(_performer_name(item) for item in _list(scene.get("performers")))
        artwork = tuple(
            Artwork.model_validate({"url": url, "kind": "thumb"})
            for item in _list(scene.get("images"))
            if (url := _mapping_text(item, "url")) and url.startswith(("http://", "https://"))
        )
        fingerprints = {
            algorithm.casefold(): hash_value
            for item in _list(scene.get("fingerprints"))
            if (algorithm := _mapping_text(item, "algorithm")) and (hash_value := _mapping_text(item, "hash"))
        }
        director = _text(scene.get("director"))
        return ProviderRecord(
            provider=self.descriptor.id,
            external_id=external_id,
            source_url=f"https://theporndb.net/scenes/{external_id}",
            code=code,
            title=title,
            family=family,
            release_date=_date(_text(scene.get("date"))),
            runtime_seconds=_integer(scene.get("duration")),
            studio=studio,
            plot=_text(scene.get("details")),
            actors=tuple(value for value in actors if value),
            directors=(director,) if director else (),
            tags=tuple(value for value in tags if value),
            artwork=artwork,
            fingerprints=fingerprints,
            language="en",
        )


def _nested(value: dict[str, object], *keys: str) -> object:
    current: object = value
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def _list(value: object) -> list[object]:
    return value if isinstance(value, list) else []


def _text(value: object) -> str | None:
    return value.strip() if isinstance(value, str) and value.strip() else None


def _mapping_text(value: object, key: str) -> str | None:
    return _text(value.get(key)) if isinstance(value, dict) else None


def _performer_name(value: object) -> str | None:
    if not isinstance(value, dict):
        return None
    nested = value.get("performer")
    return _mapping_text(nested, "name") or _mapping_text(value, "as")


def _integer(value: object) -> int | None:
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str) and value.isdigit():
        return int(value)
    return None


def _date(value: str | None) -> date | None:
    if value is None:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
