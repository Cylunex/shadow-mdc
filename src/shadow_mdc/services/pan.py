"""115 Open Platform client: device-code PKCE login, offline download, STRM hooks.

Reimplemented from public Open API shapes (Apache 115-sdk-go / Open Platform docs).
Do not vendor GPL Miyabi/OpenList sources. Magnets stay local; offline is separate.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import secrets
import time as time_module
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from ..media.strm import write_strm

logger = logging.getLogger(__name__)

# Temporary community OpenList-compatible app id — prefer your own app at open.115.com.
DEFAULT_PAN_CLIENT_ID = "100197303"
PASSPORT_URL = "https://passportapi.115.com"
QRCODE_URL = "https://qrcodeapi.115.com"
PROAPI_URL = "https://proapi.115.com"
# ~4 req/s safe ceiling for 115 Open (Miyabi-style hygiene; reimplemented).
RATE_LIMIT_SECONDS = 0.25
MAX_IN_FLIGHT = 2
REQUEST_RETRIES = 3
LOGIN_TTL_SECONDS = 300
OFFLINE_EXISTS_CODE = 10008


class PanClient(Protocol):
    """Minimal surface for cloud pan integration."""

    async def list_directory(self, directory_id: str) -> object: ...

    async def enqueue_remote_urls(self, urls: list[str], *, directory_id: str) -> object: ...


class PanNotConfiguredError(RuntimeError):
    """Raised when pan features are invoked before credentials/config."""


class PanApiError(RuntimeError):
    """115 Open API returned an error."""

    def __init__(self, message: str, *, code: int | None = None) -> None:
        super().__init__(message)
        self.code = code


class PanOfflineExistsError(PanApiError):
    """115 reports the offline task already exists (typically code 10008)."""


class PanOfflineConflictError(PanApiError):
    """Offline task exists but cannot be safely reused (wrong dir / incomplete)."""


class PanCredentials(BaseModel):
    model_config = ConfigDict(extra="ignore")

    access_token: str
    refresh_token: str
    expires_at: str  # ISO-8601 UTC
    user_id: str | None = None
    user_name: str | None = None


class PanSettings(BaseModel):
    model_config = ConfigDict(extra="forbid")

    offline_directory_id: str | None = None
    strm_enabled: bool = False
    strm_output_root: str | None = None
    strm_url_prefix: str = "http://openlist:5244/d/115"
    use_proxy: bool = False
    # Optional per-instance app credentials; unset values fall back to environment defaults.
    client_id: str | None = None
    client_secret: str | None = None


class CredentialStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> PanCredentials | None:
        if not self._path.is_file():
            return None
        try:
            return PanCredentials.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return None

    def save(self, credentials: PanCredentials) -> PanCredentials:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(credentials.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        return credentials

    def clear(self) -> None:
        if self._path.is_file():
            self._path.unlink()


class PanConfigStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> PanSettings:
        if not self._path.is_file():
            return PanSettings()
        try:
            return PanSettings.model_validate_json(self._path.read_text(encoding="utf-8-sig"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return PanSettings()

    def save(self, settings: PanSettings) -> PanSettings:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(settings.model_dump_json(indent=2) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        return settings


def generate_pkce() -> tuple[str, str]:
    """Return (code_verifier, code_challenge).

    verifier = base64url(48 random bytes); challenge = standard base64(SHA256(verifier)).
    """

    verifier = base64.urlsafe_b64encode(secrets.token_bytes(48)).decode("ascii").rstrip("=")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    challenge = base64.b64encode(digest).decode("ascii")
    return verifier, challenge


def map_remote_status(status: int | None) -> str:
    """Map 115 offline status to local: running | done | failed."""

    if status is None:
        return "running"
    if status in (0, 1):
        return "running"
    if status == 2:
        return "done"
    if status == -1:
        return "failed"
    return "running"


def _parse_progress(value: Any) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return 0.0
    return 0.0


def parse_retry_after(header: str | None) -> float:
    """Return seconds to wait from a Retry-After header (delta-seconds or HTTP-date)."""

    if not header:
        return 0.0
    raw = header.strip()
    if not raw:
        return 0.0
    try:
        seconds = int(raw)
    except ValueError:
        seconds = -1
    if seconds > 0:
        return float(seconds)
    try:
        from email.utils import parsedate_to_datetime

        when = parsedate_to_datetime(raw)
        if when.tzinfo is None:
            when = when.replace(tzinfo=UTC)
        wait = (when - datetime.now(UTC)).total_seconds()
        return wait if wait > 0 else 0.0
    except (TypeError, ValueError, OverflowError):
        return 0.0


def is_offline_exists_code(code: Any) -> bool:
    try:
        return int(code) == OFFLINE_EXISTS_CODE
    except (TypeError, ValueError):
        return False


def _is_video_name(name: str) -> bool:
    lowered = name.casefold()
    return any(
        lowered.endswith(ext)
        for ext in (".mp4", ".mkv", ".avi", ".wmv", ".ts", ".m2ts", ".mov", ".flv", ".webm")
    )


def _unwrap_data(payload: dict[str, Any]) -> Any:
    if "data" in payload:
        return payload["data"]
    return payload


def _check_api_ok(payload: dict[str, Any], *, context: str) -> None:
    if "state" in payload and payload.get("state") is False:
        raise PanApiError(
            f"{context}: {payload.get('message') or payload.get('error') or 'request failed'}",
            code=int(payload["code"]) if isinstance(payload.get("code"), int) else None,
        )
    code = payload.get("code")
    # Some endpoints use code!=0 with state true; only fail when clearly errored.
    if (
        isinstance(code, int)
        and code not in (0, 200)
        and payload.get("state") is not True
        and (payload.get("state") is False or "error" in payload)
    ):
        raise PanApiError(
            f"{context}: {payload.get('message') or payload.get('error') or f'code {code}'}",
            code=code,
        )


@dataclass
class _LoginSession:
    id: str
    uid: str
    time: int
    sign: str
    verifier: str
    qr_png_b64: str
    created_at: float = field(default_factory=time_module.monotonic)
    state: str = "waiting"  # waiting|scanned|ok|expired|canceled|error
    error: str | None = None


class Pan115Client:
    """httpx client for 115 Open Platform. Prefer direct egress (no proxy) by default."""

    def __init__(
        self,
        *,
        client_id: str,
        client_secret: str | None = None,
        proxy_url: str | None = None,
        use_proxy: bool = False,
        user_agent: str = "ShadowMDC/0.1",
        timeout: float = 30.0,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self._proxy = proxy_url if use_proxy and proxy_url else None
        self._user_agent = user_agent
        self._timeout = timeout
        self._lock = asyncio.Lock()
        self._inflight = asyncio.Semaphore(MAX_IN_FLIGHT)
        self._last_call = 0.0
        self._retry_after_until = 0.0
        self._http: httpx.AsyncClient | None = None
        self._access_token: str | None = None
        self._refresh_token: str | None = None
        self._expires_at: datetime | None = None
        self._on_tokens: Any = None

    def bind_tokens(
        self,
        credentials: PanCredentials | None,
        *,
        on_tokens: Any = None,
    ) -> None:
        self._on_tokens = on_tokens
        if credentials is None:
            self._access_token = None
            self._refresh_token = None
            self._expires_at = None
            return
        self._access_token = credentials.access_token
        self._refresh_token = credentials.refresh_token
        try:
            self._expires_at = datetime.fromisoformat(credentials.expires_at)
        except ValueError:
            self._expires_at = None

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=True,
                headers={"User-Agent": self._user_agent},
                proxy=self._proxy,
            )
        return self._http

    async def _throttle(self) -> None:
        """Enforce spacing and global Retry-After backoff before a request start."""

        while True:
            async with self._lock:
                now = time_module.monotonic()
                wait_ra = self._retry_after_until - now
                wait_gap = RATE_LIMIT_SECONDS - (now - self._last_call)
                wait = max(wait_ra, wait_gap, 0.0)
                if wait <= 0:
                    self._last_call = time_module.monotonic()
                    return
            await asyncio.sleep(wait)

    def _note_retry_after(self, response: httpx.Response) -> float:
        wait = parse_retry_after(response.headers.get("Retry-After"))
        if wait <= 0:
            return 0.0
        until = time_module.monotonic() + wait
        # Best-effort without await; races only extend backoff.
        if until > self._retry_after_until:
            self._retry_after_until = until
        return wait

    async def _request(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
        bearer: bool = False,
        raw: bool = False,
    ) -> httpx.Response:
        http = self._ensure_http()
        headers: dict[str, str] = {}
        if bearer:
            if not self._access_token:
                raise PanNotConfiguredError("115 access token missing")
            headers["Authorization"] = f"Bearer {self._access_token}"

        async with self._inflight:
            last: httpx.Response | None = None
            for attempt in range(REQUEST_RETRIES + 1):
                await self._throttle()
                response = await http.request(
                    method,
                    url,
                    data=form,
                    params=params,
                    headers=headers,
                )
                last = response
                ra = self._note_retry_after(response)
                if response.status_code in {429, 502, 503, 504} and attempt < REQUEST_RETRIES:
                    delay = ra if ra > 0 else float(attempt + 1)
                    await asyncio.sleep(delay)
                    continue
                return response
            assert last is not None
            return last

    async def begin_device_login(self) -> tuple[str, str, int, str, bytes]:
        """Start PKCE device login. Returns (verifier, uid, time, sign, qr_png)."""

        verifier, challenge = generate_pkce()
        response = await self._request(
            "POST",
            f"{PASSPORT_URL}/open/authDeviceCode",
            form={
                "client_id": self.client_id,
                "code_challenge": challenge,
                "code_challenge_method": "sha256",
            },
        )
        response.raise_for_status()
        payload = response.json()
        _check_api_ok(payload if isinstance(payload, dict) else {}, context="authDeviceCode")
        data = _unwrap_data(payload) if isinstance(payload, dict) else {}
        if not isinstance(data, dict):
            raise PanApiError("authDeviceCode: unexpected response")
        uid = str(data.get("uid") or "")
        sign = str(data.get("sign") or "")
        time_val = data.get("time")
        if not uid or not sign or time_val is None:
            raise PanApiError("authDeviceCode: missing uid/time/sign")
        time_int = int(time_val)
        qr = await self._request(
            "GET",
            f"{QRCODE_URL}/api/1.0/web/1.0/qrcode",
            params={"uid": uid},
            raw=True,
        )
        qr.raise_for_status()
        if not qr.content.startswith(b"\x89PNG"):
            # Some gateways wrap JSON errors; surface briefly without leaking secrets.
            raise PanApiError("qrcode: expected PNG image")
        return verifier, uid, time_int, sign, qr.content

    async def poll_login_status(self, *, uid: str, time_value: int, sign: str) -> str:
        """Return waiting|scanned|ok|expired|canceled."""

        response = await self._request(
            "GET",
            f"{QRCODE_URL}/get/status/",
            params={"uid": uid, "time": str(time_value), "sign": sign},
        )
        # Expired/canceled may still be HTTP 200 with status field.
        try:
            payload = response.json()
        except ValueError:
            return "waiting"
        if not isinstance(payload, dict):
            return "waiting"
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        status = data.get("status") if isinstance(data, dict) else None
        if status is None:
            return "waiting"
        try:
            code = int(status)
        except (TypeError, ValueError):
            return "waiting"
        if code == 0:
            return "waiting"
        if code == 1:
            return "scanned"
        if code == 2:
            return "ok"
        if code == -1:
            return "expired"
        if code == -2:
            return "canceled"
        return "waiting"

    async def exchange_token(self, *, uid: str, code_verifier: str) -> PanCredentials:
        response = await self._request(
            "POST",
            f"{PASSPORT_URL}/open/deviceCodeToToken",
            form={"uid": uid, "code_verifier": code_verifier},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PanApiError("deviceCodeToToken: unexpected response")
        _check_api_ok(payload, context="deviceCodeToToken")
        data = _unwrap_data(payload)
        if not isinstance(data, dict):
            raise PanApiError("deviceCodeToToken: missing data")
        return self._credentials_from_token_payload(data)

    async def refresh_access_token(self) -> PanCredentials:
        if not self._refresh_token:
            raise PanNotConfiguredError("115 refresh token missing")
        response = await self._request(
            "POST",
            f"{PASSPORT_URL}/open/refreshToken",
            form={"refresh_token": self._refresh_token},
        )
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PanApiError("refreshToken: unexpected response")
        _check_api_ok(payload, context="refreshToken")
        data = _unwrap_data(payload)
        if not isinstance(data, dict):
            raise PanApiError("refreshToken: missing data")
        credentials = self._credentials_from_token_payload(data)
        self.bind_tokens(credentials, on_tokens=self._on_tokens)
        if self._on_tokens:
            self._on_tokens(credentials)
        return credentials

    def _credentials_from_token_payload(self, data: dict[str, Any]) -> PanCredentials:
        access = data.get("access_token")
        refresh = data.get("refresh_token")
        expires_in = data.get("expires_in")
        if not isinstance(access, str) or not isinstance(refresh, str):
            raise PanApiError("token response missing access_token/refresh_token")
        seconds = int(expires_in) if isinstance(expires_in, (int, float, str)) else 7200
        try:
            seconds = int(expires_in)  # type: ignore[arg-type]
        except (TypeError, ValueError):
            seconds = 7200
        expires_at = datetime.now(UTC) + timedelta(seconds=max(60, seconds))
        self._access_token = access
        self._refresh_token = refresh
        self._expires_at = expires_at
        return PanCredentials(
            access_token=access,
            refresh_token=refresh,
            expires_at=expires_at.isoformat(),
            user_id=str(data["user_id"]) if data.get("user_id") is not None else None,
            user_name=str(data["user_name"]) if isinstance(data.get("user_name"), str) else None,
        )

    async def ensure_access_token(self) -> None:
        if not self._access_token:
            raise PanNotConfiguredError("115 not logged in")
        # Refresh slightly early when expiry is known.
        if self._expires_at is not None and datetime.now(UTC) + timedelta(seconds=120) < self._expires_at:
            return
        await self.refresh_access_token()

    async def _authed_json(
        self,
        method: str,
        url: str,
        *,
        form: dict[str, str] | None = None,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        await self.ensure_access_token()
        response = await self._request(method, url, form=form, params=params, bearer=True)
        if response.status_code == 401:
            await self.refresh_access_token()
            response = await self._request(method, url, form=form, params=params, bearer=True)
        response.raise_for_status()
        payload = response.json()
        if not isinstance(payload, dict):
            raise PanApiError(f"{url}: unexpected response")
        if payload.get("state") is False or (
            isinstance(payload.get("code"), int)
            and payload["code"] not in (0, 200)
            and payload.get("state") is not True
        ):
            # Token expired codes — try refresh once.
            code = payload.get("code")
            if code in (99, 40140116, 40140117, 40140119) or (
                isinstance(code, int) and 40100000 <= code < 40200000
            ):
                await self.refresh_access_token()
                response = await self._request(method, url, form=form, params=params, bearer=True)
                response.raise_for_status()
                payload = response.json()
                if not isinstance(payload, dict):
                    raise PanApiError(f"{url}: unexpected response after refresh")
            else:
                message = str(payload.get("message") or payload.get("error") or f"API error code={code}")
                code_int = int(code) if isinstance(code, int) else None
                if is_offline_exists_code(code_int):
                    raise PanOfflineExistsError(message, code=OFFLINE_EXISTS_CODE)
                raise PanApiError(message, code=code_int)
        return payload

    async def fetch_user_info(self) -> dict[str, Any]:
        payload = await self._authed_json("GET", f"{PROAPI_URL}/open/user/info")
        data = _unwrap_data(payload)
        return data if isinstance(data, dict) else {}

    async def list_directory(
        self,
        directory_id: str = "0",
        *,
        page: int = 1,
        limit: int = 100,
    ) -> dict[str, Any]:
        offset = max(0, (max(1, page) - 1) * limit)
        payload = await self._authed_json(
            "GET",
            f"{PROAPI_URL}/open/ufile/files",
            params={
                "cid": directory_id or "0",
                "offset": str(offset),
                "limit": str(limit),
                "show_dir": "1",
                "stdir": "1",
                "cur": "1",
                "o": "file_name",
                "asc": "1",
            },
        )
        # AuthRequestRaw-style: list may live under data, count at top-level.
        items_raw = payload.get("data")
        if not isinstance(items_raw, list):
            items_raw = []
        entries: list[dict[str, Any]] = []
        for item in items_raw:
            if not isinstance(item, dict):
                continue
            fid = str(item.get("fid") or item.get("cid") or "")
            name = str(item.get("fn") or item.get("file_name") or "")
            fc = str(item.get("fc") or "")
            is_dir = fc in ("0", "folder") or (item.get("fid") is None and item.get("cid") is not None)
            # Open API: fc 0 folder, 1 file (string).
            if fc == "0":
                is_dir = True
            elif fc == "1":
                is_dir = False
            entries.append(
                {
                    "id": fid,
                    "name": name,
                    "is_directory": is_dir,
                    "size": int(item["fs"]) if isinstance(item.get("fs"), int) else None,
                    "pick_code": item.get("pc"),
                    "parent_id": str(item["pid"]) if item.get("pid") is not None else None,
                }
            )
        count = payload.get("count")
        total = int(count) if isinstance(count, (int, float, str)) and str(count).isdigit() else len(entries)
        return {
            "directory_id": str(payload.get("cid") or directory_id or "0"),
            "page": page,
            "limit": limit,
            "total": total,
            "items": entries,
            "path": payload.get("path") if isinstance(payload.get("path"), list) else [],
        }

    async def enqueue_remote_urls(self, urls: list[str], *, directory_id: str) -> list[dict[str, Any]]:
        if not urls:
            raise ValueError("urls is empty")
        joined = "\n".join(urls)
        payload = await self._authed_json(
            "POST",
            f"{PROAPI_URL}/open/offline/add_task_urls",
            form={"urls": joined, "wp_path_id": directory_id},
        )
        data = _unwrap_data(payload)
        results: list[dict[str, Any]] = []
        if isinstance(data, list):
            for item in data:
                if isinstance(item, dict):
                    results.append(
                        {
                            "state": bool(item.get("state", True)),
                            "info_hash": str(item.get("info_hash") or ""),
                            "url": str(item.get("url") or ""),
                            "message": str(item.get("message") or ""),
                            "code": item.get("code"),
                        }
                    )
        elif isinstance(data, dict) and data.get("info_hash"):
            results.append(
                {
                    "state": True,
                    "info_hash": str(data["info_hash"]),
                    "url": urls[0] if urls else "",
                    "message": "",
                    "code": 0,
                }
            )
        for item in results:
            if item.get("state"):
                continue
            if is_offline_exists_code(item.get("code")):
                raise PanOfflineExistsError(
                    str(item.get("message") or "115 offline task already exists"),
                    code=OFFLINE_EXISTS_CODE,
                )
        return results

    async def remove_offline_history(self, info_hash: str) -> None:
        """Clear offline *history* only; never delete cloud source files."""

        digest = (info_hash or "").strip()
        if not digest:
            raise ValueError("info_hash is empty")
        await self._authed_json(
            "POST",
            f"{PROAPI_URL}/open/offline/del_task",
            form={"info_hash": digest, "del_source_file": "0"},
        )

    async def find_remote_offline_task(self, info_hash: str, *, max_pages: int = 20) -> dict[str, Any] | None:
        digest = (info_hash or "").strip().upper()
        if not digest:
            return None
        for page in range(1, max(1, max_pages) + 1):
            listing = await self.get_task_list(page=page)
            for task in listing.get("tasks") or []:
                if not isinstance(task, dict):
                    continue
                if str(task.get("info_hash") or "").upper() == digest:
                    return task
            page_count = int(listing.get("page_count") or 1)
            if page >= page_count:
                break
        return None

    async def remote_has_video(self, file_id: str) -> bool:
        """Best-effort check whether a completed offline result still has video content."""

        fid = (file_id or "").strip()
        if not fid:
            return False
        try:
            info = await self.get_folder_info(fid)
        except PanApiError as exc:
            if exc.code == 430004:
                return False
            # Unknown: treat as present so we do not wipe history blindly.
            logger.warning("115 folder info check failed: %s", type(exc).__name__)
            return True
        except Exception:
            logger.warning("115 folder info check failed", exc_info=True)
            return True
        name = str(info.get("file_name") or info.get("fn") or info.get("name") or "")
        # Open API folder/get_info: directories often carry file_category / fc.
        fc = str(info.get("file_category") or info.get("fc") or "")
        is_dir = fc in {"0", "folder"} or bool(info.get("is_dir"))
        if not is_dir and name:
            return _is_video_name(name)
        # Shallow list of the folder; do not deep-walk entire libraries.
        try:
            listing = await self.list_directory(fid, page=1, limit=100)
        except Exception:
            return True
        for entry in listing.get("items") or []:
            if not isinstance(entry, dict):
                continue
            if entry.get("is_directory"):
                continue
            if _is_video_name(str(entry.get("name") or "")):
                return True
        return False

    async def submit_offline_url(
        self,
        url: str,
        *,
        directory_id: str,
        info_hash_hint: str | None = None,
    ) -> dict[str, Any]:
        """Submit one offline URL with duplicate / wrong-dir / stale-history reconcile.

        Reimplemented from public Open API shapes (not GPL source). Returns a result
        dict with info_hash and optional adopted remote task fields.
        """

        try:
            results = await self.enqueue_remote_urls([url], directory_id=directory_id)
        except PanOfflineExistsError:
            results = None
        else:
            if not results:
                raise PanApiError("115 offline submit returned no result")
            first = results[0]
            if first.get("state") or first.get("info_hash"):
                digest = str(first.get("info_hash") or info_hash_hint or "").upper()
                if not digest:
                    raise PanApiError("115 offline submit missing info_hash")
                return {
                    "info_hash": digest,
                    "state": True,
                    "message": str(first.get("message") or ""),
                    "adopted": False,
                    "remote": None,
                }
            code = first.get("code") if isinstance(first.get("code"), int) else None
            raise PanApiError(
                str(first.get("message") or "115 offline submit rejected"),
                code=code,
            )

        hint = (info_hash_hint or "").strip().upper()
        if not hint:
            # Try extract from magnet URI if present.
            from ..media.magnets import info_hash_from_uri

            extracted = info_hash_from_uri(url)
            hint = (extracted or "").upper()
        if not hint:
            raise PanOfflineExistsError(
                "115 offline task already exists, but info_hash is unknown",
                code=OFFLINE_EXISTS_CODE,
            )

        remote = await self.find_remote_offline_task(hint)
        if remote is None:
            raise PanOfflineConflictError(
                "115 提示任务已存在，但任务列表中未找到它，请稍后重试",  # noqa: RUF001
                code=OFFLINE_EXISTS_CODE,
            )

        status = remote.get("status")
        remote_dir = str(remote.get("directory_id") or "") or None
        file_id = str(remote.get("file_id") or "") or None
        digest = str(remote.get("info_hash") or hint).upper()

        if status in (0, 1):
            if remote_dir and remote_dir != str(directory_id):
                raise PanOfflineConflictError(
                    "115 已有该磁力的下载任务，目标目录与当前离线目录不一致",  # noqa: RUF001
                    code=OFFLINE_EXISTS_CODE,
                )
            return {
                "info_hash": digest,
                "state": True,
                "message": "adopted running remote task",
                "adopted": True,
                "remote": remote,
            }

        if status not in (2, -1):
            raise PanOfflineConflictError(
                f"115 returned unknown offline status {status}",
                code=OFFLINE_EXISTS_CODE,
            )

        if not file_id:
            raise PanOfflineConflictError(
                "115 的历史任务未提供资源位置，请先在 115 客户端清理该任务记录",  # noqa: RUF001
                code=OFFLINE_EXISTS_CODE,
            )

        present = await self.remote_has_video(file_id)
        if present:
            if status == -1:
                raise PanOfflineConflictError(
                    "115 任务失败但目录内仍有视频，请先在 115 客户端确认完整性",  # noqa: RUF001
                    code=OFFLINE_EXISTS_CODE,
                )
            return {
                "info_hash": digest,
                "state": True,
                "message": "adopted completed remote task",
                "adopted": True,
                "remote": remote,
            }

        # Stale history without files: clear history only, then resubmit.
        await self.remove_offline_history(digest)
        results = await self.enqueue_remote_urls([url], directory_id=directory_id)
        if not results:
            raise PanApiError("115 offline resubmit returned no result")
        first = results[0]
        if not first.get("state") and not first.get("info_hash"):
            code = first.get("code") if isinstance(first.get("code"), int) else None
            raise PanApiError(
                str(first.get("message") or "115 offline resubmit rejected"),
                code=code,
            )
        new_hash = str(first.get("info_hash") or digest).upper()
        return {
            "info_hash": new_hash,
            "state": True,
            "message": "cleared stale history and resubmitted",
            "adopted": False,
            "remote": None,
        }

    async def get_task_list(self, page: int = 1) -> dict[str, Any]:
        payload = await self._authed_json(
            "GET",
            f"{PROAPI_URL}/open/offline/get_task_list",
            params={"page": str(max(1, page))},
        )
        data = _unwrap_data(payload)
        if not isinstance(data, dict):
            data = payload
        tasks_raw = data.get("tasks") if isinstance(data, dict) else None
        if not isinstance(tasks_raw, list):
            tasks_raw = []
        tasks: list[dict[str, Any]] = []
        for item in tasks_raw:
            if not isinstance(item, dict):
                continue
            status_raw = item.get("status")
            try:
                status_int = int(status_raw) if status_raw is not None else None
            except (TypeError, ValueError):
                status_int = None
            tasks.append(
                {
                    "info_hash": str(item.get("info_hash") or ""),
                    "name": str(item.get("name") or ""),
                    "status": status_int,
                    "local_status": map_remote_status(status_int),
                    "progress": _parse_progress(item.get("percentDone")),
                    "file_id": str(item.get("file_id") or "") or None,
                    "directory_id": str(item.get("wp_path_id") or "") or None,
                    "size": int(item["size"]) if isinstance(item.get("size"), int) else None,
                    "url": str(item.get("url") or "") or None,
                }
            )
        return {
            "page": int(data.get("page") or page) if isinstance(data, dict) else page,
            "page_count": int(data["page_count"]) if isinstance(data.get("page_count"), int) else 1,
            "count": int(data["count"]) if isinstance(data.get("count"), int) else len(tasks),
            "tasks": tasks,
        }

    async def get_folder_info(self, file_id: str) -> dict[str, Any]:
        payload = await self._authed_json(
            "GET",
            f"{PROAPI_URL}/open/folder/get_info",
            params={"file_id": file_id},
        )
        data = _unwrap_data(payload)
        return data if isinstance(data, dict) else {}


class PanService:
    """Orchestrates login sessions, credentials, config, and 115 client."""

    def __init__(
        self,
        *,
        data_dir: Path,
        client_id: str = DEFAULT_PAN_CLIENT_ID,
        client_secret: str | None = None,
        proxy_url: str | None = None,
        user_agent: str = "ShadowMDC/0.1",
    ):
        pan_dir = data_dir / "pan"
        pan_dir.mkdir(parents=True, exist_ok=True)
        self.credentials_store = CredentialStore(pan_dir / "credentials.json")
        self.config_store = PanConfigStore(pan_dir / "config.json")
        self.client_id = client_id or DEFAULT_PAN_CLIENT_ID
        self.client_secret = client_secret
        self.proxy_url = proxy_url
        self.user_agent = user_agent
        self._logins: dict[str, _LoginSession] = {}
        self._client: Pan115Client | None = None
        self._submit_locks: dict[str, asyncio.Lock] = {}
        self._submit_locks_guard = asyncio.Lock()

    def _config(self) -> PanSettings:
        return self.config_store.load()

    def _persist_credentials(self, credentials: PanCredentials) -> None:
        existing = self.credentials_store.load()
        merged = credentials
        if existing is not None:
            merged = credentials.model_copy(
                update={
                    "user_id": credentials.user_id or existing.user_id,
                    "user_name": credentials.user_name or existing.user_name,
                }
            )
        self.credentials_store.save(merged)

    def _effective_client_id(self, cfg: PanSettings | None = None) -> str:
        settings = cfg or self._config()
        return settings.client_id or self.client_id or DEFAULT_PAN_CLIENT_ID

    def _effective_client_secret(self, cfg: PanSettings | None = None) -> str | None:
        settings = cfg or self._config()
        return settings.client_secret if settings.client_secret is not None else self.client_secret

    def settings_status(self) -> dict[str, object]:
        cfg = self._config()
        return {
            **cfg.model_dump(exclude={"client_id", "client_secret"}),
            "client_id": self._effective_client_id(cfg),
            "client_secret_set": bool(self._effective_client_secret(cfg)),
        }

    def get_client(self) -> Pan115Client:
        cfg = self._config()
        if self._client is None:
            self._client = Pan115Client(
                client_id=self._effective_client_id(cfg),
                client_secret=self._effective_client_secret(cfg),
                proxy_url=self.proxy_url,
                use_proxy=cfg.use_proxy,
                user_agent=self.user_agent,
            )

            self._client.bind_tokens(self.credentials_store.load(), on_tokens=self._persist_credentials)
        else:
            # Keep use_proxy in sync if config changed.
            self._client._proxy = self.proxy_url if cfg.use_proxy and self.proxy_url else None
        return self._client

    def status(self) -> dict[str, object]:
        creds = self.credentials_store.load()
        cfg = self._config()
        configured = creds is not None and bool(creds.access_token and creds.refresh_token)
        directory_set = bool(cfg.offline_directory_id)
        if not configured:
            return {
                "provider": "115",
                "configured": False,
                "available": False,
                "reason": (
                    "Not logged in. Use Settings → 115 QR login "
                    f"(client_id={self._effective_client_id(cfg)}; prefer your own app at open.115.com)."
                ),
                "client_id": self._effective_client_id(cfg),
                "offline_directory_id": cfg.offline_directory_id,
                "strm_enabled": cfg.strm_enabled,
                "connected": False,
            }
        return {
            "provider": "115",
            "configured": True,
            "available": True,
            "reason": (
                "Connected."
                if directory_set
                else "Connected; set offline directory id before submitting tasks."
            ),
            "client_id": self._effective_client_id(cfg),
            "offline_directory_id": cfg.offline_directory_id,
            "strm_enabled": cfg.strm_enabled,
            "strm_output_root": cfg.strm_output_root,
            "strm_url_prefix": cfg.strm_url_prefix,
            "connected": True,
            "account": {
                "user_id": creds.user_id if creds else None,
                "user_name": creds.user_name if creds else None,
                "expires_at": creds.expires_at if creds else None,
            },
        }

    def import_tokens(
        self,
        access_token: str,
        refresh_token: str,
        *,
        expires_in: int | None = None,
        user_id: str | None = None,
        user_name: str | None = None,
    ) -> dict[str, object]:
        """Import OpenList-compatible tokens without exposing them in the result."""

        access = access_token.strip()
        refresh = refresh_token.strip()
        if not access or not refresh:
            raise ValueError("access_token and refresh_token are required")
        seconds = 7200 if expires_in is None else max(60, int(expires_in))
        credentials = PanCredentials(
            access_token=access,
            refresh_token=refresh,
            expires_at=(datetime.now(UTC) + timedelta(seconds=seconds)).isoformat(),
            user_id=user_id,
            user_name=user_name,
        )
        self.credentials_store.save(credentials)
        self.get_client().bind_tokens(credentials, on_tokens=self._persist_credentials)
        return self.status()

    async def start_login(self) -> dict[str, str]:
        self._purge_logins()
        client = self.get_client()
        verifier, uid, time_value, sign, png = await client.begin_device_login()
        login_id = str(uuid.uuid4())
        session = _LoginSession(
            id=login_id,
            uid=uid,
            time=time_value,
            sign=sign,
            verifier=verifier,
            qr_png_b64=base64.b64encode(png).decode("ascii"),
        )
        self._logins[login_id] = session
        return {"id": login_id, "qr_code": session.qr_png_b64}

    async def login_status(self, login_id: str) -> dict[str, object]:
        session = self._logins.get(login_id)
        if session is None:
            return {"state": "expired", "error": "unknown login id"}
        if time_module.monotonic() - session.created_at > LOGIN_TTL_SECONDS:
            session.state = "expired"
            return {"state": "expired"}
        if session.state in {"ok", "expired", "canceled", "error"}:
            return {"state": session.state, "error": session.error}
        client = self.get_client()
        try:
            state = await client.poll_login_status(
                uid=session.uid, time_value=session.time, sign=session.sign
            )
        except Exception as exc:
            logger.warning("115 login poll failed: %s", type(exc).__name__)
            return {"state": session.state}
        session.state = state
        if state == "ok":
            try:
                credentials = await client.exchange_token(
                    uid=session.uid, code_verifier=session.verifier
                )
                # Enrich account meta when possible.
                client.bind_tokens(credentials)
                try:
                    info = await client.fetch_user_info()
                    credentials = credentials.model_copy(
                        update={
                            "user_id": (
                                str(info.get("user_id") or info.get("uid") or "")
                                or credentials.user_id
                            ),
                            "user_name": (
                                str(info.get("user_name") or info.get("name") or "")
                                or credentials.user_name
                            ),
                        }
                    )
                except Exception:
                    pass
                self.credentials_store.save(credentials)
                client.bind_tokens(credentials, on_tokens=self._persist_credentials)
            except Exception as exc:
                session.state = "error"
                session.error = f"{type(exc).__name__}: {exc}"
                logger.warning("115 token exchange failed: %s", type(exc).__name__)
        return {"state": session.state, "error": session.error}

    def disconnect(self) -> None:
        self.credentials_store.clear()
        if self._client is not None:
            self._client.bind_tokens(None)
        self._logins.clear()

    def account(self) -> dict[str, object]:
        status = self.status()
        cfg = self._config()
        return {
            "connected": bool(status.get("connected")),
            "account": status.get("account"),
            "directory": {"id": cfg.offline_directory_id} if cfg.offline_directory_id else None,
            "strm": {
                "enabled": cfg.strm_enabled,
                "output_root": cfg.strm_output_root,
                "url_prefix": cfg.strm_url_prefix,
            },
            "use_proxy": cfg.use_proxy,
            "client_id": self._effective_client_id(cfg),
            "client_secret_set": bool(self._effective_client_secret(cfg)),
        }

    def save_settings(self, patch: dict[str, Any]) -> PanSettings:
        current = self._config()
        data = current.model_dump()
        for key in (
            "offline_directory_id",
            "strm_enabled",
            "strm_output_root",
            "strm_url_prefix",
            "use_proxy",
            "client_id",
            "client_secret",
        ):
            if key in patch:
                value = patch[key]
                if key == "client_id" and isinstance(value, str):
                    value = value.strip() or None
                if key == "client_secret" and isinstance(value, str):
                    value = value.strip() or None
                data[key] = value
        old_client_id = self._effective_client_id(current)
        saved = self.config_store.save(PanSettings.model_validate(data))
        new_client_id = self._effective_client_id(saved)
        if new_client_id != old_client_id:
            self.credentials_store.clear()
            self._logins.clear()
            self.client_id = new_client_id
            if self._client is not None:
                self._client.bind_tokens(None)
                self._client.client_id = new_client_id
                self._client.client_secret = self._effective_client_secret(saved)
        elif self._client is not None:
            self._client.client_secret = self._effective_client_secret(saved)
        # Keep use_proxy in sync if config changed.
        if self._client is not None and "use_proxy" in patch:
            self._client._proxy = self.proxy_url if saved.use_proxy and self.proxy_url else None
        return saved

    def set_directory(self, directory_id: str) -> PanSettings:
        return self.save_settings({"offline_directory_id": directory_id.strip() or None})

    def _purge_logins(self) -> None:
        now = time_module.monotonic()
        expired = [key for key, item in self._logins.items() if now - item.created_at > LOGIN_TTL_SECONDS]
        for key in expired:
            self._logins.pop(key, None)

    async def _hash_lock(self, info_hash: str) -> asyncio.Lock:
        key = (info_hash or "").strip().upper() or "__empty__"
        async with self._submit_locks_guard:
            lock = self._submit_locks.get(key)
            if lock is None:
                lock = asyncio.Lock()
                self._submit_locks[key] = lock
            return lock

    async def submit_offline_url(
        self,
        url: str,
        *,
        directory_id: str,
        info_hash_hint: str | None = None,
    ) -> dict[str, Any]:
        """Per-hash locked offline submit with duplicate reconcile."""

        from ..media.magnets import info_hash_from_uri

        hint = (info_hash_hint or info_hash_from_uri(url) or "").upper()
        lock = await self._hash_lock(hint or url)
        async with lock:
            client = self.get_client()
            return await client.submit_offline_url(
                url, directory_id=directory_id, info_hash_hint=hint or None
            )

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None


def build_strm_locator(*, prefix: str, relative_path: str) -> str:
    base = prefix.rstrip("/")
    rel = relative_path.lstrip("/")
    return f"{base}/{rel}" if rel else base


def write_offline_strm(
    *,
    settings: PanSettings,
    work_code: str | None,
    file_name: str | None,
    remote_relative: str | None = None,
) -> str | None:
    """Write `{root}/{CODE}/{CODE}.strm` or best-effort from file name. Returns path or None."""

    if not settings.strm_enabled:
        return None
    root = settings.strm_output_root
    if not root:
        return None
    code = (work_code or "").strip() or None
    name = (file_name or "").strip() or None
    folder = code or (Path(name).stem if name else "offline")
    stem = code or (Path(name).stem if name else "offline")
    # Prefer known relative path under OpenList/115; else file name only.
    if remote_relative:
        locator = build_strm_locator(prefix=settings.strm_url_prefix, relative_path=remote_relative)
    elif name:
        locator = build_strm_locator(prefix=settings.strm_url_prefix, relative_path=name)
    else:
        locator = build_strm_locator(prefix=settings.strm_url_prefix, relative_path=f"{stem}.mp4")
    path = Path(root) / folder / f"{stem}.strm"
    write_strm(path, locator)
    return str(path)


def pan_status() -> dict[str, object]:
    """Backward-compatible helper when no PanService is wired (tests / early boot)."""

    return {
        "provider": "115",
        "configured": False,
        "available": False,
        "reason": "Pan service not initialized.",
        "connected": False,
    }


def resolve_client_id(explicit: str | None = None) -> str:
    value = (explicit or os.environ.get("SHADOW_MDC_PAN_CLIENT_ID") or DEFAULT_PAN_CLIENT_ID).strip()
    return value or DEFAULT_PAN_CLIENT_ID


def resolve_client_secret() -> str | None:
    value = os.environ.get("SHADOW_MDC_PAN_CLIENT_SECRET")
    if value is None or not value.strip():
        return None
    return value.strip()
