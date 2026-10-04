"""OpenList (formerly AList) pan backend.

Talks to a user-run OpenList instance that already mounts 115 storage, so
shadow-mdc never touches 115 credentials itself. Re-implemented from OpenList's
public HTTP API shapes (https://docs.oplist.org); no OpenList (AGPL) code is
vendored.

Endpoints used:

* ``POST /api/auth/login`` → ``data.token`` (only when username/password is used)
* ``GET  /api/me``                                      (test connection)
* ``POST /api/fs/list`` / ``POST /api/fs/get``           (walk + sign / raw_url)
* ``POST /api/fs/add_offline_download``                 (``tool`` = ``115 Cloud`` …)
* ``GET  /api/task/offline_download/{undone,done}``     (status)
* ``GET  /api/task/offline_download_transfer/undone``   (post-download move)

Every response is HTTP 200 with ``{"code": 200, "message": …, "data": …}``; a
non-200 ``code`` is an error. Tokens/passwords are never logged or returned.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import time as time_module
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import parse_qs, quote, urlsplit

import httpx
from pydantic import BaseModel, ConfigDict

from ..media.magnets import info_hash_from_uri
from .pan_common import (
    AuthRejectionStore,
    BackoffGate,
    PanApiError,
    PanAuthRejectedError,
    PanNotConfiguredError,
    PanOfflineConflictError,
    RemoteVideo,
    ScanPacer,
    SingleFlight,
    is_video_name,
    shared_gate,
)

logger = logging.getLogger(__name__)

DEFAULT_OPENLIST_TOOL = "115 Cloud"
OPENLIST_TOOLS_HINT = ("115 Cloud", "115 Open")
DEFAULT_DELETE_POLICY = "delete_on_upload_succeed"
DELETE_POLICIES = frozenset(
    {
        "delete_on_upload_succeed",
        "delete_on_upload_failed",
        "delete_never",
        "delete_always",
        "upload_download_stream",
    }
)
# OpenList is a LAN service; keep a light global spacing anyway (~5 req/s) and
# reuse the 115 cold-walk ScanPacer for directory walks (the 115 driver behind
# OpenList still hits 115 for uncached listings).
OPENLIST_MIN_INTERVAL = 0.2
OPENLIST_MAX_IN_FLIGHT = 2
OPENLIST_RETRIES = 2
# OpenList JWTs default to 48h; re-login a bit earlier.
SESSION_TTL = timedelta(hours=24)
LIST_PAGE_SIZE = 200
# /api/auth/login answers that need the user to fix credentials (wrong user /
# password, OTP required, account disabled). 429 / 5xx stay transient.
LOGIN_REJECT_CODES = frozenset({400, 401, 402, 403})
REJECTION_KEY = "openlist"

# tache task states (OpenList task manager).
STATE_PENDING = 0
STATE_RUNNING = 1
STATE_SUCCEEDED = 2
STATE_CANCELING = 3
STATE_CANCELED = 4
STATE_ERRORED = 5
STATE_FAILING = 6
STATE_FAILED = 7
STATE_WAITING_RETRY = 8
STATE_BEFORE_RETRY = 9

_TASK_NAME_RE = re.compile(r"^download (?P<url>.+) to \((?P<dst>.*)\)$", re.S)
_TRANSFER_DST_RE = re.compile(r" to \[(?P<mount>[^\]]*)\]\((?P<path>[^)]*)\)\s*$")
_EXISTS_MARKERS = ("already exist", "exists", "已存在", "重复", "10008")


class OpenListApiError(PanApiError):
    """OpenList answered with a non-200 ``code`` (message is server-provided)."""


class OpenListAuthError(OpenListApiError):
    """Token rejected / login failed."""


def map_openlist_state(state: Any) -> str:
    """Map a tache state to the local queue vocabulary: running | done | failed."""

    try:
        value = int(state)
    except (TypeError, ValueError):
        return "running"
    if value == STATE_SUCCEEDED:
        return "done"
    if value in (STATE_CANCELED, STATE_FAILED):
        return "failed"
    return "running"


def normalize_openlist_path(value: str | None) -> str | None:
    """``/115/云下载`` style absolute POSIX path, no trailing slash; None if empty."""

    if value is None:
        return None
    raw = value.strip().replace("\\", "/")
    if not raw:
        return None
    if not raw.startswith("/"):
        raw = "/" + raw
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if ".." in parts:
        raise ValueError("OpenList path must not contain '..'")
    return "/" + "/".join(parts)


def normalize_base_url(value: str | None) -> str | None:
    if value is None:
        return None
    raw = value.strip().rstrip("/")
    if not raw:
        return None
    parsed = urlsplit(raw)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("OpenList URL must be an absolute http(s) URL, e.g. http://192.168.0.2:5244")
    if parsed.query or parsed.fragment:
        raise ValueError("OpenList URL must not contain a query or fragment")
    return raw


def path_within(child: str, parent: str) -> bool:
    child_norm = child.rstrip("/") or "/"
    parent_norm = parent.rstrip("/") or "/"
    if parent_norm == "/":
        return child_norm.startswith("/")
    return child_norm == parent_norm or child_norm.startswith(parent_norm + "/")


def build_openlist_d_url(base_url: str, path: str, sign: str | None = None) -> str:
    """``{base}/d{percent-encoded path}[?sign=…]`` — the OpenList direct (302) link."""

    base = normalize_base_url(base_url)
    if not base:
        raise ValueError("OpenList base URL is not configured")
    if not path.startswith("/"):
        raise ValueError("OpenList path must be absolute")
    url = f"{base}/d{quote(path, safe='/')}"
    if sign:
        url = f"{url}?sign={quote(sign, safe='')}"
    return url


def offline_info_hash(url: str) -> str:
    """Stable dedup key: magnet btih, else sha1 of the URL (http/ed2k)."""

    digest = info_hash_from_uri(url)
    if digest:
        return digest.upper()
    return hashlib.sha1(url.strip().encode("utf-8")).hexdigest().upper()


def magnet_display_name(url: str | None) -> str | None:
    if not url or not url.lower().startswith("magnet:?"):
        return None
    try:
        values = parse_qs(url[len("magnet:?") :]).get("dn")
    except ValueError:
        return None
    if not values:
        return None
    name = values[0].strip()
    return name or None


def code_pattern(code: str | None) -> re.Pattern[str] | None:
    """Loose, boundary-aware matcher for a work code inside a release name.

    ``SONE-118`` matches ``SONE-118.mp4``, ``[x] sone118-C``, ``SONE_00118`` but not
    ``SONE-1180`` or ``XSONE-118``.
    """

    if not code:
        return None
    chunks = re.findall(r"[A-Za-z]+|\d+", code)
    if not chunks:
        return None
    pieces: list[str] = []
    for chunk in chunks:
        if chunk.isdigit():
            pieces.append("0*" + re.escape(chunk.lstrip("0") or "0"))
        else:
            pieces.append(re.escape(chunk))
    body = r"[-_ .]*".join(pieces)
    tail = r"(?!\d)" if chunks[-1].isdigit() else r"(?![A-Za-z])"
    return re.compile(r"(?<![A-Za-z0-9])" + body + tail, re.IGNORECASE)


@dataclass(frozen=True, slots=True)
class OpenListEntry:
    name: str
    path: str
    is_dir: bool
    size: int | None = None
    modified: datetime | None = None
    sign: str | None = None


@dataclass(frozen=True, slots=True)
class OpenListTask:
    id: str
    name: str
    state: int | None
    status: str
    progress: float
    error: str
    url: str | None = None
    dst: str | None = None
    info_hash: str | None = None

    @property
    def local_status(self) -> str:
        return map_openlist_state(self.state)


@dataclass(slots=True)
class OpenListTaskSnapshot:
    by_id: dict[str, OpenListTask] = field(default_factory=dict)
    by_hash: dict[str, OpenListTask] = field(default_factory=dict)
    pending_transfer_dsts: list[str] = field(default_factory=list)


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    raw = value.strip()
    if raw.endswith("Z"):
        raw = raw[:-1] + "+00:00"
    # Go may emit nanoseconds; Python handles up to microseconds.
    raw = re.sub(r"(\.\d{6})\d+", r"\1", raw)
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    if parsed.year < 1971:
        return None
    return parsed


def parse_task(item: dict[str, Any]) -> OpenListTask:
    name = str(item.get("name") or "")
    url: str | None = None
    dst: str | None = None
    match = _TASK_NAME_RE.match(name)
    if match:
        url = match.group("url").strip()
        dst = match.group("dst").strip() or None
    state_raw = item.get("state")
    try:
        state = int(state_raw) if state_raw is not None else None
    except (TypeError, ValueError):
        state = None
    progress_raw = item.get("progress")
    try:
        progress = float(progress_raw) if progress_raw is not None else 0.0
    except (TypeError, ValueError):
        progress = 0.0
    return OpenListTask(
        id=str(item.get("id") or ""),
        name=name,
        state=state,
        status=str(item.get("status") or ""),
        progress=max(0.0, min(100.0, progress)),
        error=str(item.get("error") or ""),
        url=url,
        dst=dst,
        info_hash=offline_info_hash(url) if url else None,
    )


def _entry_from_obj(parent: str, item: dict[str, Any]) -> OpenListEntry | None:
    name = item.get("name")
    if not isinstance(name, str) or not name or "/" in name:
        return None
    size = item.get("size")
    sign = item.get("sign")
    base = parent.rstrip("/")
    return OpenListEntry(
        name=name,
        path=f"{base}/{name}",
        is_dir=bool(item.get("is_dir")),
        size=int(size) if isinstance(size, (int, float)) else None,
        modified=_parse_time(item.get("modified")),
        sign=sign if isinstance(sign, str) and sign else None,
    )


def looks_like_exists_error(message: str) -> bool:
    lowered = message.casefold()
    return any(marker in lowered for marker in _EXISTS_MARKERS)


def looks_like_not_found(message: str) -> bool:
    lowered = message.casefold()
    if "storage" in lowered:
        # Unmounted storage must never be read as "files deleted".
        return False
    return "object not found" in lowered or "file not found" in lowered or lowered.endswith("not found")


class OpenListCredentials(BaseModel):
    model_config = ConfigDict(extra="ignore")

    token: str | None = None
    username: str | None = None
    password: str | None = None
    # Cached JWT from /api/auth/login (username/password mode only).
    session_token: str | None = None
    session_expires_at: str | None = None

    def has_auth(self) -> bool:
        return bool(self.token or (self.username and self.password))


class OpenListCredentialStore:
    """0600 JSON file next to the 115 credentials; never echoed by the API."""

    def __init__(self, path: Path):
        self._path = path

    def load(self) -> OpenListCredentials:
        if not self._path.is_file():
            return OpenListCredentials()
        try:
            return OpenListCredentials.model_validate_json(self._path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError, json.JSONDecodeError):
            return OpenListCredentials()

    def save(self, credentials: OpenListCredentials) -> OpenListCredentials:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(credentials.model_dump_json(indent=2) + "\n")
        temporary.replace(self._path)
        with contextlib.suppress(OSError):
            os.chmod(self._path, 0o600)
        return credentials

    def clear(self) -> None:
        if self._path.is_file():
            self._path.unlink()


@dataclass(frozen=True, slots=True)
class OpenListConfig:
    base_url: str | None
    offline_path: str | None
    tool: str = DEFAULT_OPENLIST_TOOL
    delete_policy: str = DEFAULT_DELETE_POLICY
    strm_base_url: str | None = None
    strm_sign: bool = False


class OpenListClient:
    """Thin async OpenList API client with pacing, bounded retries and re-login."""

    def __init__(
        self,
        *,
        base_url: str,
        credentials: OpenListCredentialStore,
        timeout: float = 20.0,
        transport: httpx.AsyncBaseTransport | None = None,
        min_interval: float = OPENLIST_MIN_INTERVAL,
        user_agent: str = "ShadowMDC/0.1",
        gate: BackoffGate | None = None,
        rejections: AuthRejectionStore | None = None,
    ) -> None:
        normalized = normalize_base_url(base_url)
        if not normalized:
            raise PanNotConfiguredError("OpenList base URL is not configured")
        self.base_url = normalized
        self._credentials = credentials
        self._timeout = timeout
        self._transport = transport
        self._min_interval = min_interval
        self._user_agent = user_agent
        self._http: httpx.AsyncClient | None = None
        self._lock = asyncio.Lock()
        self._inflight = asyncio.Semaphore(OPENLIST_MAX_IN_FLIGHT)
        self._last_call = 0.0
        # Process-wide Retry-After gate shared by every OpenList caller (and kept
        # across client re-creation when the base URL changes).
        self.gate = gate or shared_gate("openlist")
        self._rejections = rejections
        # Concurrent logins (several callers seeing an expired session) share one.
        self._login_flight: SingleFlight[str] = SingleFlight()

    def _reject(self, detail: str) -> PanAuthRejectedError:
        if self._rejections is not None:
            self._rejections.mark(REJECTION_KEY, detail)
        logger.warning("OpenList rejected the stored credentials: %s", detail)
        return PanAuthRejectedError(f"OpenList needs re-login: {detail}")

    async def aclose(self) -> None:
        if self._http is not None:
            await self._http.aclose()
            self._http = None

    def _ensure_http(self) -> httpx.AsyncClient:
        if self._http is None:
            # LAN service: never route through HTTP(S)_PROXY from the environment.
            self._http = httpx.AsyncClient(
                timeout=self._timeout,
                follow_redirects=False,
                headers={"User-Agent": self._user_agent},
                transport=self._transport,
                trust_env=False,
            )
        return self._http

    async def _throttle(self) -> None:
        while True:
            honoured = await self.gate.wait()
            async with self._lock:
                if self.gate.deadline > honoured:
                    continue
                now = time_module.monotonic()
                wait = self._min_interval - (now - self._last_call)
                if wait <= 0:
                    self._last_call = time_module.monotonic()
                    return
            await asyncio.sleep(wait)

    # ---------------------------------------------------------------- auth

    async def _auth_header(self, *, force_login: bool = False) -> str:
        if self._rejections is not None:
            rejection = self._rejections.get(REJECTION_KEY)
            if rejection is not None:
                # Do not retry rejected credentials until the user replaces them.
                raise PanAuthRejectedError(f"OpenList needs re-login: {rejection.detail}")
        creds = self._credentials.load()
        if creds.token and not force_login:
            return creds.token
        if not (creds.username and creds.password):
            if creds.token:
                return creds.token
            raise PanNotConfiguredError("OpenList token or username/password is not configured")
        if not force_login and creds.session_token and creds.session_expires_at:
            try:
                expires = datetime.fromisoformat(creds.session_expires_at)
            except ValueError:
                expires = datetime.now(UTC)
            if expires > datetime.now(UTC):
                return creds.session_token
        return await self.login()

    async def login(self) -> str:
        """Username/password → JWT via ``/api/auth/login``; cached in the credential store.

        Single-flight: concurrent callers share one login request. A permanent
        refusal (wrong password, OTP required …) clears the cached session,
        records "needs re-login" and raises :class:`PanAuthRejectedError`.
        """

        return await self._login_flight.run("login", self._login_once)

    async def _login_once(self) -> str:
        creds = self._credentials.load()
        if not (creds.username and creds.password):
            raise PanNotConfiguredError("OpenList username/password is not configured")
        try:
            data = await self._call(
                "POST",
                "/api/auth/login",
                json_body={"username": creds.username, "password": creds.password},
                auth=False,
                idempotent=False,
            )
        except OpenListAuthError as exc:
            if exc.code in LOGIN_REJECT_CODES:
                self._drop_session()
                raise self._reject(str(exc)) from exc
            raise
        token = data.get("token") if isinstance(data, dict) else None
        if not isinstance(token, str) or not token:
            raise OpenListAuthError("OpenList login returned no token")
        latest = self._credentials.load()
        if (latest.username, latest.password) != (creds.username, creds.password):
            # Credentials changed mid-login: do not cache a token for the old ones.
            return token
        self._credentials.save(
            latest.model_copy(
                update={
                    "session_token": token,
                    "session_expires_at": (datetime.now(UTC) + SESSION_TTL).isoformat(),
                }
            )
        )
        return token

    def _drop_session(self) -> None:
        creds = self._credentials.load()
        if creds.session_token:
            cleared = creds.model_copy(update={"session_token": None, "session_expires_at": None})
            self._credentials.save(cleared)

    # ------------------------------------------------------------- request

    async def _call(
        self,
        method: str,
        path: str,
        *,
        json_body: dict[str, Any] | None = None,
        params: dict[str, str] | None = None,
        auth: bool = True,
        idempotent: bool = True,
        _relogged: bool = False,
    ) -> Any:
        http = self._ensure_http()
        headers: dict[str, str] = {}
        if auth:
            headers["Authorization"] = await self._auth_header()
        response: httpx.Response | None = None
        async with self._inflight:
            for attempt in range(OPENLIST_RETRIES + 1):
                await self._throttle()
                try:
                    response = await http.request(
                        method, f"{self.base_url}{path}", json=json_body, params=params, headers=headers
                    )
                except httpx.TransportError as exc:
                    if idempotent and attempt < OPENLIST_RETRIES:
                        await asyncio.sleep(float(attempt + 1))
                        continue
                    raise OpenListApiError(f"OpenList unreachable: {type(exc).__name__}") from exc
                wait = self.gate.note(response.status_code, response.headers.get("Retry-After"))
                retryable = response.status_code == 429 or (
                    idempotent and response.status_code in {502, 503, 504}
                )
                if retryable and attempt < OPENLIST_RETRIES:
                    if wait <= 0:
                        await asyncio.sleep(float(attempt + 1))
                    # Otherwise the shared gate (awaited in _throttle) holds every caller.
                    continue
                break
        assert response is not None
        if response.status_code == 401 or response.status_code == 403:
            payload: Any = None
        else:
            if response.status_code >= 400:
                raise OpenListApiError(f"OpenList HTTP {response.status_code}", code=response.status_code)
            try:
                payload = response.json()
            except ValueError as exc:
                raise OpenListApiError("OpenList returned non-JSON (wrong base URL?)") from exc
        code: int | None
        if payload is None:
            code = response.status_code
            message = "unauthorized"
        else:
            if not isinstance(payload, dict):
                raise OpenListApiError("OpenList returned an unexpected payload")
            raw_code = payload.get("code")
            code = int(raw_code) if isinstance(raw_code, (int, float)) else None
            message = str(payload.get("message") or "")
            if code == 200:
                return payload.get("data")
        if code in (401, 403) and auth:
            creds = self._credentials.load()
            used = headers.get("Authorization")
            if code == 401 and creds.token and creds.token == used:
                # Static API token rejected: clear it right away so it is never
                # retried. Fall back to username/password once when present.
                self._credentials.save(creds.model_copy(update={"token": None}))
                creds = self._credentials.load()
                if not (creds.username and creds.password) or _relogged:
                    raise self._reject(f"OpenList rejected the API token: {message or code}")
            elif code == 401 and _relogged:
                # A freshly issued session was refused too: stop instead of looping.
                self._drop_session()
                raise self._reject(f"OpenList rejected a fresh login session: {message or code}")
            if not _relogged and not creds.token and creds.username and creds.password:
                self._drop_session()
                await self.login()
                return await self._call(
                    method,
                    path,
                    json_body=json_body,
                    params=params,
                    auth=auth,
                    idempotent=idempotent,
                    _relogged=True,
                )
            raise OpenListAuthError(f"OpenList rejected credentials: {message or code}", code=code)
        if path == "/api/auth/login":
            # 400/401 wrong password, 402 OTP required, 429 too many attempts.
            raise OpenListAuthError(f"OpenList login failed: {message or code}", code=code)
        raise OpenListApiError(message or f"OpenList error code={code}", code=code)

    # ------------------------------------------------------------ endpoints

    async def me(self) -> dict[str, Any]:
        data = await self._call("GET", "/api/me")
        return data if isinstance(data, dict) else {}

    async def offline_tools(self) -> list[str]:
        data = await self._call("GET", "/api/public/offline_download_tools", auth=False)
        return [str(item) for item in data] if isinstance(data, list) else []

    async def list_dir(
        self,
        path: str,
        *,
        page: int = 1,
        per_page: int = LIST_PAGE_SIZE,
        refresh: bool = False,
    ) -> tuple[list[OpenListEntry], int]:
        data = await self._call(
            "POST",
            "/api/fs/list",
            json_body={"path": path, "password": "", "page": page, "per_page": per_page, "refresh": refresh},
        )
        if not isinstance(data, dict):
            return [], 0
        content = data.get("content") or []
        entries = [
            entry
            for item in content
            if isinstance(item, dict) and (entry := _entry_from_obj(path, item)) is not None
        ]
        total_raw = data.get("total")
        total = int(total_raw) if isinstance(total_raw, (int, float)) else len(entries)
        return entries, total

    async def probe_dir(self, path: str) -> tuple[int | None, bool | None]:
        """One-item listing: (total entries, write permission) for the test button."""

        data = await self._call(
            "POST",
            "/api/fs/list",
            json_body={"path": path, "password": "", "page": 1, "per_page": 1, "refresh": False},
        )
        if not isinstance(data, dict):
            return None, None
        total = data.get("total")
        write = data.get("write")
        return (
            int(total) if isinstance(total, (int, float)) else None,
            write if isinstance(write, bool) else None,
        )

    async def get(self, path: str) -> dict[str, Any]:
        data = await self._call("POST", "/api/fs/get", json_body={"path": path, "password": ""})
        return data if isinstance(data, dict) else {}

    async def add_offline_download(
        self, urls: list[str], *, path: str, tool: str, delete_policy: str
    ) -> list[OpenListTask]:
        # Never retried: OpenList may already have created the 115 task.
        data = await self._call(
            "POST",
            "/api/fs/add_offline_download",
            json_body={"urls": urls, "path": path, "tool": tool, "delete_policy": delete_policy},
            idempotent=False,
        )
        tasks = data.get("tasks") if isinstance(data, dict) else None
        return [parse_task(item) for item in tasks or [] if isinstance(item, dict)]

    async def task_list(self, kind: str, *, done: bool) -> list[OpenListTask]:
        if kind not in {"offline_download", "offline_download_transfer"}:
            raise ValueError("unsupported task kind")
        data = await self._call("GET", f"/api/task/{kind}/{'done' if done else 'undone'}")
        return [parse_task(item) for item in data or [] if isinstance(item, dict)]


class OpenListService:
    """Backend orchestration: submit with dedup, task snapshot, result match, walk, exists."""

    def __init__(
        self,
        *,
        credentials: OpenListCredentialStore,
        config_loader: Callable[[], OpenListConfig],
        transport: httpx.AsyncBaseTransport | None = None,
        user_agent: str = "ShadowMDC/0.1",
        rejections: AuthRejectionStore | None = None,
    ) -> None:
        self.credentials = credentials
        self.rejections = rejections
        self._config_loader = config_loader
        self._transport = transport
        self._user_agent = user_agent
        self._client: OpenListClient | None = None
        self._sign_cache: dict[str, tuple[float, str | None]] = {}

    def config(self) -> OpenListConfig:
        return self._config_loader()

    def configured(self) -> bool:
        cfg = self.config()
        return bool(cfg.base_url) and self.credentials.load().has_auth() and not self.needs_relogin()

    def needs_relogin(self) -> bool:
        return self.rejections is not None and self.rejections.get(REJECTION_KEY) is not None

    def client(self) -> OpenListClient:
        cfg = self.config()
        if not cfg.base_url:
            raise PanNotConfiguredError("OpenList base URL is not configured")
        if self._client is None or self._client.base_url != normalize_base_url(cfg.base_url):
            old = self._client
            self._client = OpenListClient(
                base_url=cfg.base_url,
                credentials=self.credentials,
                transport=self._transport,
                user_agent=self._user_agent,
                rejections=self.rejections,
            )
            if old is not None:
                # Best-effort close of the client for a previous base URL.
                with contextlib.suppress(RuntimeError):
                    asyncio.get_running_loop().create_task(old.aclose())
        return self._client

    def reset_session(self) -> None:
        self._sign_cache.clear()

    def set_transport(self, transport: httpx.AsyncBaseTransport | None) -> None:
        """Swap the HTTP transport (tests / mocks); the next call builds a fresh client."""

        self._transport = transport
        self._client = None

    async def aclose(self) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------- tasks

    async def task_snapshot(self) -> OpenListTaskSnapshot:
        client = self.client()
        snapshot = OpenListTaskSnapshot()
        for done in (False, True):
            for task in await client.task_list("offline_download", done=done):
                if not task.id:
                    continue
                snapshot.by_id[task.id] = task
                if task.info_hash:
                    current = snapshot.by_hash.get(task.info_hash)
                    # Prefer an undone task over an older finished one.
                    if current is None or current.local_status != "running":
                        snapshot.by_hash[task.info_hash] = task
        try:
            for task in await client.task_list("offline_download_transfer", done=False):
                match = _TRANSFER_DST_RE.search(task.name)
                if match:
                    mount = match.group("mount").rstrip("/")
                    snapshot.pending_transfer_dsts.append(f"{mount}/{match.group('path').lstrip('/')}")
        except (OpenListApiError, PanNotConfiguredError):
            # Transfer listing is advisory only.
            logger.debug("OpenList transfer task listing failed", exc_info=True)
        return snapshot

    async def submit_offline(
        self,
        url: str,
        *,
        target_path: str,
        info_hash_hint: str | None = None,
        work_code: str | None = None,
    ) -> dict[str, Any]:
        """Submit one URL with OpenList-side dedup/reconcile.

        Order: adopt a matching undone task → adopt a succeeded task for the same
        target → submit → on "already exists", adopt an existing result folder
        under the target path (by work code / magnet name) or refuse with 409.
        """

        cfg = self.config()
        client = self.client()
        digest = (info_hash_hint or offline_info_hash(url)).upper()
        try:
            snapshot = await self.task_snapshot()
        except (OpenListApiError, PanNotConfiguredError) as exc:
            if isinstance(exc, (OpenListAuthError, PanNotConfiguredError)):
                raise
            snapshot = OpenListTaskSnapshot()
        existing = snapshot.by_hash.get(digest)
        if existing is not None:
            same_target = existing.dst is None or path_within(existing.dst, target_path)
            if existing.local_status == "running":
                if not same_target:
                    raise PanOfflineConflictError(
                        "OpenList 已有该磁力的离线任务，但目标目录与当前设置不一致",  # noqa: RUF001
                        code=409,
                    )
                return self._result(digest, existing.id, adopted=True, message="adopted running task")
            if existing.local_status == "done" and same_target:
                return self._result(digest, existing.id, adopted=True, message="adopted finished task")
        try:
            tasks = await client.add_offline_download(
                [url], path=target_path, tool=cfg.tool, delete_policy=cfg.delete_policy
            )
        except OpenListApiError as exc:
            if isinstance(exc, OpenListAuthError) or not looks_like_exists_error(str(exc)):
                raise
            entry = await self.find_result(
                target_path, work_code=work_code, display_name=magnet_display_name(url), since=None
            )
            if entry is None:
                raise PanOfflineConflictError(
                    "115 提示该离线任务已存在，但 OpenList 目标目录下未找到对应结果；"  # noqa: RUF001
                    "请在 115 清理该离线记录或检查目标路径",
                    code=409,
                ) from exc
            return self._result(digest, None, adopted=True, message="adopted existing result folder")
        if not tasks or not tasks[0].id:
            raise OpenListApiError("OpenList offline submit returned no task")
        return self._result(digest, tasks[0].id, adopted=False, message="submitted")

    @staticmethod
    def _result(digest: str, task_id: str | None, *, adopted: bool, message: str) -> dict[str, Any]:
        return {
            "info_hash": digest,
            "state": True,
            "message": message,
            "adopted": adopted,
            "remote": None,
            "remote_task_id": task_id,
            "backend": "openlist",
        }

    # ------------------------------------------------------------ listing

    async def list_all(
        self,
        path: str,
        *,
        pacer: ScanPacer | None = None,
        refresh: bool = False,
        max_entries: int = 5000,
    ) -> list[OpenListEntry]:
        client = self.client()
        pace = pacer or ScanPacer()
        found: list[OpenListEntry] = []
        page = 1
        while True:
            await pace.wait()
            entries, total = await client.list_dir(path, page=page, refresh=refresh and page == 1)
            found.extend(entries)
            if not entries or page * LIST_PAGE_SIZE >= total or len(found) >= max_entries:
                return found[:max_entries]
            page += 1

    async def find_result(
        self,
        target_path: str,
        *,
        work_code: str | None,
        display_name: str | None,
        since: datetime | None,
        pacer: ScanPacer | None = None,
    ) -> OpenListEntry | None:
        """Locate the folder/file a finished offline task produced under ``target_path``.

        OpenList does not report the 115 result name, so match (in order): exact
        magnet ``dn`` name → work-code pattern → (only when nothing else) the
        newest entry modified after the task started.
        """

        entries = await self.list_all(target_path, pacer=pacer, refresh=True)
        if not entries:
            return None
        if display_name:
            wanted = display_name.casefold()
            for entry in entries:
                if entry.name.casefold() == wanted or PurePosixPath(entry.name).stem.casefold() == wanted:
                    return entry
        pattern = code_pattern(work_code)
        if pattern is not None:
            matches = [entry for entry in entries if pattern.search(entry.name)]
            if matches:
                return max(matches, key=lambda item: item.modified or datetime.min.replace(tzinfo=UTC))
            return None
        if since is not None:
            fresh = [entry for entry in entries if entry.modified and entry.modified >= since]
            if len(fresh) == 1:
                return fresh[0]
        return None

    async def walk_videos(
        self,
        root: OpenListEntry | str,
        *,
        pacer: ScanPacer | None = None,
        max_depth: int = 3,
        max_entries: int = 2000,
    ) -> list[RemoteVideo]:
        """Video files under a path; ``file_id`` and ``relative_path`` are the full OpenList path."""

        pace = pacer or ScanPacer()
        if isinstance(root, str):
            await pace.wait()
            info = await self.client().get(root)
            name = str(info.get("name") or PurePosixPath(root).name)
            sign = info.get("sign")
            size = info.get("size")
            root_entry = OpenListEntry(
                name=name,
                path=root,
                is_dir=bool(info.get("is_dir")),
                size=int(size) if isinstance(size, (int, float)) else None,
                sign=sign if isinstance(sign, str) and sign else None,
            )
        else:
            root_entry = root
        if not root_entry.is_dir:
            if not is_video_name(root_entry.name):
                return []
            return [_video(root_entry)]
        found: list[RemoteVideo] = []
        queue: list[tuple[str, int]] = [(root_entry.path, 0)]
        seen = 0
        while queue:
            folder, depth = queue.pop(0)
            for entry in await self.list_all(folder, pacer=pace):
                seen += 1
                if seen > max_entries:
                    return found
                if entry.is_dir:
                    if depth + 1 <= max_depth:
                        queue.append((entry.path, depth + 1))
                    continue
                if is_video_name(entry.name):
                    found.append(_video(entry))
        return found

    async def exists(self, path: str) -> bool | None:
        """True/False when definitive; None for auth/network/storage problems."""

        try:
            info = await self.client().get(path)
        except OpenListApiError as exc:
            if isinstance(exc, OpenListAuthError):
                return None
            return False if looks_like_not_found(str(exc)) else None
        except PanNotConfiguredError:
            return None
        return True if info else None

    async def sign_for(self, path: str, *, ttl: float = 600.0) -> str | None:
        now = time_module.monotonic()
        cached = self._sign_cache.get(path)
        if cached and cached[0] > now:
            return cached[1]
        info = await self.client().get(path)
        sign = info.get("sign")
        value = sign if isinstance(sign, str) and sign else None
        self._sign_cache[path] = (now + ttl, value)
        if len(self._sign_cache) > 2048:
            self._sign_cache.pop(next(iter(self._sign_cache)))
        return value

    async def test_connection(self) -> dict[str, Any]:
        """``/api/me`` + list the offline target; never returns secrets."""

        cfg = self.config()
        result: dict[str, Any] = {
            "ok": False,
            "base_url": cfg.base_url,
            "user": None,
            "base_path": None,
            "permission": None,
            "target_path": cfg.offline_path,
            "target_ok": None,
            "target_entries": None,
            "target_writable": None,
            "tool": cfg.tool,
            "tools": None,
            "tool_available": None,
            "detail": None,
        }
        if not cfg.base_url:
            result["detail"] = "OpenList base URL is not configured"
            return result
        if not self.credentials.load().has_auth():
            result["detail"] = "OpenList token or username/password is not configured"
            return result
        client = self.client()
        try:
            me = await client.me()
        except (OpenListApiError, PanNotConfiguredError) as exc:
            result["detail"] = f"/api/me failed: {exc}"
            return result
        result["user"] = me.get("username")
        result["base_path"] = me.get("base_path")
        result["permission"] = me.get("permission")
        try:
            tools = await client.offline_tools()
            result["tools"] = tools
            result["tool_available"] = cfg.tool in tools if tools else None
        except OpenListApiError:
            result["tools"] = None
        if cfg.offline_path:
            try:
                total, writable = await client.probe_dir(cfg.offline_path)
                result["target_ok"] = True
                result["target_entries"] = total
                result["target_writable"] = writable
            except OpenListApiError as exc:
                result["target_ok"] = False
                result["detail"] = f"/api/fs/list {cfg.offline_path} failed: {exc}"
                return result
        result["ok"] = True
        if result["tool_available"] is False:
            result["detail"] = (
                f"tool '{cfg.tool}' is not offered by this OpenList (available: {result['tools']})"
            )
        elif not cfg.offline_path:
            result["detail"] = "Connected; set the OpenList offline target path"
        else:
            result["detail"] = "Connected"
        return result


def _video(entry: OpenListEntry) -> RemoteVideo:
    return RemoteVideo(
        file_id=entry.path,
        name=entry.name,
        pick_code=None,
        relative_path=entry.path,
        size=entry.size,
        sign=entry.sign,
    )
