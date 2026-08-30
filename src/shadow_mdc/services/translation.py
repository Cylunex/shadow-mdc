import hashlib
import json
import re
import sqlite3
from pathlib import Path

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


class GoogleTitleTranslator:
    def __init__(
        self,
        client: httpx.AsyncClient,
        cache: TranslationCache,
        *,
        enabled: bool,
        endpoint: str,
        target_language: str,
    ):
        self._client = client
        self._cache = cache
        self._enabled = enabled
        self._endpoint = endpoint
        self._target_language = target_language

    async def translate_work(self, repository: Repository, work: Work) -> TranslationResult:
        source = work.original_title or work.title
        if not self._enabled:
            return TranslationResult(work_id=work.id, status="skipped", source=source, detail="disabled")
        if (work.field_sources or {}).get("title") in {"local-manual", "local-path"}:
            return TranslationResult(
                work_id=work.id,
                status="skipped",
                source=source,
                detail="local title",
            )
        if not needs_translation(source, work.family):
            return TranslationResult(
                work_id=work.id,
                status="skipped",
                source=source,
                detail="already target language",
            )
        cached = self._cache.get(source, self._target_language, "title")
        try:
            translated = cached or await self._request(source)
        except (httpx.HTTPError, ValueError, json.JSONDecodeError) as exc:
            return TranslationResult(
                work_id=work.id,
                status="failed",
                source=source,
                detail=f"{type(exc).__name__}: {exc}",
            )
        if not translated or translated.casefold() == source.casefold():
            return TranslationResult(
                work_id=work.id,
                status="failed",
                source=source,
                detail="translation response was empty or unchanged",
            )
        if cached is None:
            self._cache.put(source, self._target_language, "title", translated)
        repository.apply_title_translation(work, source=source, translated=translated, provider="google")
        return TranslationResult(
            work_id=work.id,
            status="translated",
            source=source,
            translated=translated,
        )

    async def _request(self, source: str) -> str:
        response = await self._client.get(
            self._endpoint,
            params={
                "client": "gtx",
                "sl": "auto",
                "tl": self._target_language,
                "dt": "t",
                "q": source,
            },
        )
        response.raise_for_status()
        return _translated_text(response.json())


def needs_translation(value: str, family: str) -> bool:
    if family == "chinese":
        return False
    if _KANA.search(value) or _HANGUL.search(value):
        return True
    if _HAN.search(value) and not _LATIN.search(value):
        return False
    return bool(_LATIN.search(value))


def _translated_text(payload: object) -> str:
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


def _cache_key(source: str, target_language: str, field_name: str) -> str:
    payload = f"{target_language}\0{field_name}\0{source}".encode()
    return hashlib.sha256(payload).hexdigest()
