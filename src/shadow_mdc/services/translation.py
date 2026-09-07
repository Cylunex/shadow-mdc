"""Pluggable title/plot translation with DeepL / DeepLX / custom URL + Google fallback."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from ..db.models import Work
from ..db.repository import Repository

_KANA = re.compile(r"[\u3040-\u30ff]")
_HANGUL = re.compile(r"[\uac00-\ud7af]")
_HAN = re.compile(r"[\u3400-\u9fff]")
_LATIN = re.compile(r"[A-Za-z]{2,}")


class TranslationResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    work_id: str
    status: str
    source: str
    translated: str | None = None
    field_name: str = "title"
    provider: str | None = None
    detail: str | None = None


class TranslationCache:
    def __init__(self, path: Path):
        self._path = path
        self._initialize()

    def _initialize(self) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS translations (
                    cache_key TEXT PRIMARY KEY,
                    source_text TEXT NOT NULL,
                    target_language TEXT NOT NULL,
                    field_name TEXT NOT NULL,
                    translated_text TEXT NOT NULL
                )
                """
            )

    def get(self, source: str, target_language: str, field_name: str) -> str | None:
        key = _cache_key(source, target_language, field_name)
        with sqlite3.connect(self._path) as connection:
            row = connection.execute(
                "SELECT translated_text FROM translations WHERE cache_key = ?",
                (key,),
            ).fetchone()
        return str(row[0]) if row is not None else None

    def put(self, source: str, target_language: str, field_name: str, translated: str) -> None:
        key = _cache_key(source, target_language, field_name)
        with sqlite3.connect(self._path) as connection:
            connection.execute(
                """
                INSERT INTO translations (
                    cache_key, source_text, target_language, field_name, translated_text
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(cache_key) DO UPDATE SET translated_text = excluded.translated_text
                """,
                (key, source, target_language, field_name, translated),
            )


class TranslationBackend(Protocol):
    name: str

    async def translate(self, text: str, *, target_language: str) -> str: ...


class GoogleTranslateBackend:
    name = "google"

    def __init__(self, client: httpx.AsyncClient, endpoint: str):
        self._client = client
        self._endpoint = endpoint

    async def translate(self, text: str, *, target_language: str) -> str:
        response = await self._client.get(
            self._endpoint,
            params={"client": "gtx", "sl": "auto", "tl": target_language, "dt": "t", "q": text},
        )
        response.raise_for_status()
        return _google_translated_text(response.json())


class DeepLBackend:
    name = "deepl"

    def __init__(self, client: httpx.AsyncClient, *, api_url: str, api_key: str):
        self._client = client
        self._api_url = api_url.rstrip("/")
        self._api_key = api_key

    async def translate(self, text: str, *, target_language: str) -> str:
        target = _deepl_target(target_language)
        endpoint = self._api_url
        if not endpoint.rstrip("/").endswith("/translate"):
            endpoint = f"{endpoint.rstrip('/')}/v2/translate"
        response = await self._client.post(
            endpoint,
            headers={"Authorization": f"DeepL-Auth-Key {self._api_key}"},
            data={"text": text, "target_lang": target},
        )
        response.raise_for_status()
        payload = response.json()
        translations = payload.get("translations") if isinstance(payload, dict) else None
        if not isinstance(translations, list) or not translations:
            raise ValueError("DeepL response missing translations")
        text_out = translations[0].get("text") if isinstance(translations[0], dict) else None
        if not isinstance(text_out, str) or not text_out.strip():
            raise ValueError("DeepL response empty")
        return text_out.strip()


class DeepLXBackend:
    name = "deeplx"

    def __init__(self, client: httpx.AsyncClient, endpoint: str):
        self._client = client
        self._endpoint = endpoint

    async def translate(self, text: str, *, target_language: str) -> str:
        response = await self._client.post(
            self._endpoint,
            json={"text": text, "source_lang": "auto", "target_lang": _deepl_target(target_language)},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            data = payload.get("data")
            if isinstance(data, str) and data.strip():
                return data.strip()
            alternatives = payload.get("alternatives")
            if isinstance(alternatives, list) and alternatives and isinstance(alternatives[0], str):
                return alternatives[0].strip()
        raise ValueError("DeepLX response missing data")


class CustomUrlBackend:
    name = "custom"

    def __init__(self, client: httpx.AsyncClient, endpoint: str):
        self._client = client
        self._endpoint = endpoint

    async def translate(self, text: str, *, target_language: str) -> str:
        response = await self._client.post(
            self._endpoint,
            json={"q": text, "source": "auto", "target": target_language, "format": "text"},
        )
        response.raise_for_status()
        payload = response.json()
        if isinstance(payload, dict):
            for key in ("translatedText", "translated", "text", "data"):
                value = payload.get(key)
                if isinstance(value, str) and value.strip():
                    return value.strip()
        raise ValueError("custom translation response missing text")


class WorkTranslator:
    """Translate title and/or plot using an ordered backend chain with Google fallback."""

    def __init__(
        self,
        backends: list[TranslationBackend],
        cache: TranslationCache,
        *,
        enabled: bool,
        target_language: str,
        translate_plot: bool = True,
    ):
        self._backends = backends
        self._cache = cache
        self._enabled = enabled
        self._target_language = target_language
        self._translate_plot = translate_plot

    async def translate_work(self, repository: Repository, work: Work) -> TranslationResult:
        """Back-compat: translate title only and return a single result."""

        results = await self.translate_fields(repository, work, fields=("title",))
        return results[0]

    async def translate_fields(
        self,
        repository: Repository,
        work: Work,
        *,
        fields: tuple[str, ...] = ("title",),
        retry_failed_only: bool = False,
    ) -> list[TranslationResult]:
        out: list[TranslationResult] = []
        for field_name in fields:
            if field_name == "plot" and not self._translate_plot:
                continue
            out.append(await self._translate_field(repository, work, field_name, retry_failed_only))
        return out

    async def _translate_field(
        self,
        repository: Repository,
        work: Work,
        field_name: str,
        retry_failed_only: bool,
    ) -> TranslationResult:
        source = _field_source_text(work, field_name)
        if not self._enabled:
            return TranslationResult(
                work_id=work.id, status="skipped", source=source, field_name=field_name, detail="disabled"
            )
        sources = work.field_sources or {}
        if sources.get(field_name) in {"local-manual", "local-path"}:
            return TranslationResult(
                work_id=work.id,
                status="skipped",
                source=source,
                field_name=field_name,
                detail=f"local {field_name}",
            )
        if retry_failed_only and sources.get(field_name, "").startswith("translation:"):
            return TranslationResult(
                work_id=work.id,
                status="skipped",
                source=source,
                field_name=field_name,
                detail="already translated",
            )
        if not source or not needs_translation(source, work.family):
            return TranslationResult(
                work_id=work.id,
                status="skipped",
                source=source,
                field_name=field_name,
                detail="already target language",
            )
        cached = self._cache.get(source, self._target_language, field_name)
        if cached:
            existing_provider = str(sources.get(field_name, ""))
            if existing_provider.startswith("translation:"):
                provider = existing_provider.removeprefix("translation:")
            else:
                provider = self._backends[0].name if self._backends else "google"
                _apply_translation(
                    repository, work, field_name, source=source, translated=cached, provider=provider
                )
            return TranslationResult(
                work_id=work.id,
                status="translated",
                source=source,
                translated=cached,
                field_name=field_name,
                provider=provider,
            )
        errors: list[str] = []
        for backend in self._backends:
            try:
                translated = await backend.translate(source, target_language=self._target_language)
            except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
                errors.append(f"{backend.name}:{type(exc).__name__}")
                continue
            if not translated or translated.casefold() == source.casefold():
                errors.append(f"{backend.name}:empty")
                continue
            self._cache.put(source, self._target_language, field_name, translated)
            _apply_translation(
                repository, work, field_name, source=source, translated=translated, provider=backend.name
            )
            return TranslationResult(
                work_id=work.id,
                status="translated",
                source=source,
                translated=translated,
                field_name=field_name,
                provider=backend.name,
            )
        return TranslationResult(
            work_id=work.id,
            status="failed",
            source=source,
            field_name=field_name,
            detail="; ".join(errors) or "no backends",
        )


# Backwards-compatible name expected by existing imports/tests.
class GoogleTitleTranslator(WorkTranslator):
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: TranslationCache,
        *,
        enabled: bool,
        endpoint: str,
        target_language: str,
        extra_backends: list[TranslationBackend] | None = None,
        translate_plot: bool = True,
    ):
        backends: list[TranslationBackend] = list(extra_backends or [])
        backends.append(GoogleTranslateBackend(client, endpoint))
        super().__init__(
            backends,
            cache,
            enabled=enabled,
            target_language=target_language,
            translate_plot=translate_plot,
        )


def build_translation_backends(
    client: httpx.AsyncClient,
    *,
    google_endpoint: str,
    deepl_api_url: str | None,
    deepl_api_key: str | None,
    deeplx_endpoint: str | None,
    custom_endpoint: str | None,
    prefer: str = "google",
) -> list[TranslationBackend]:
    """Build ordered backends: preferred first, then remaining, Google always last as fallback."""

    available: dict[str, TranslationBackend] = {
        "google": GoogleTranslateBackend(client, google_endpoint),
    }
    if deepl_api_key and deepl_api_url:
        available["deepl"] = DeepLBackend(client, api_url=deepl_api_url, api_key=deepl_api_key)
    if deeplx_endpoint:
        available["deeplx"] = DeepLXBackend(client, deeplx_endpoint)
    if custom_endpoint:
        available["custom"] = CustomUrlBackend(client, custom_endpoint)
    ordered: list[TranslationBackend] = []
    if prefer in available:
        ordered.append(available.pop(prefer))
    for name in ("deepl", "deeplx", "custom", "google"):
        backend = available.pop(name, None)
        if backend is not None:
            ordered.append(backend)
    return ordered


def needs_translation(value: str, family: str) -> bool:
    if family == "chinese":
        return False
    if _KANA.search(value) or _HANGUL.search(value):
        return True
    if _HAN.search(value) and not _LATIN.search(value):
        return False
    return bool(_LATIN.search(value))


def _field_source_text(work: Work, field_name: str) -> str:
    if field_name == "plot":
        return (work.plot or "").strip()
    return (work.original_title or work.title or "").strip()


def _apply_translation(
    repository: Repository,
    work: Work,
    field_name: str,
    *,
    source: str,
    translated: str,
    provider: str,
) -> None:
    if field_name == "title":
        repository.apply_title_translation(work, source=source, translated=translated, provider=provider)
        return
    if field_name == "plot":
        repository.apply_plot_translation(work, translated=translated, provider=provider)


def _google_translated_text(payload: object) -> str:
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], list):
        raise ValueError("translation response has an unexpected shape")
    values: list[str] = []
    for segment in payload[0]:
        if isinstance(segment, list) and segment and isinstance(segment[0], str):
            values.append(segment[0])
    translated = "".join(values).strip()
    if not translated:
        raise ValueError("translation response does not contain text")
    return translated


def _deepl_target(language: str) -> str:
    lowered = language.replace("_", "-").casefold()
    if lowered in {"zh", "zh-cn", "zh-hans"}:
        return "ZH"
    if lowered in {"zh-tw", "zh-hant"}:
        return "ZH"
    if lowered.startswith("en"):
        return "EN"
    return language.split("-")[0].upper()


def _cache_key(source: str, target_language: str, field_name: str) -> str:
    payload = f"{target_language}\0{field_name}\0{source}".encode()
    return hashlib.sha256(payload).hexdigest()
