"""X / Twitter handle normalization and existence verification.

Only verified, real handles should be persisted or shown as links.
Demo/test placeholders are rejected; live checks use a lightweight public
profile GET against x.com (browser UA). Network failures are treated as
unverified so speculative handles are never stored.
"""

from __future__ import annotations

import re
import unicodedata
from typing import Final

import httpx

_HANDLE_RE = re.compile(r"[A-Za-z0-9_]{1,50}")
_BLOCKED_EXACT: Final[frozenset[str]] = frozenset(
    {
        "demouser",
        "demo_user",
        "testuser",
        "test_user",
        "fakeuser",
        "fake_user",
        "exampleuser",
        "example_user",
        "placeholder",
        "yourhandle",
        "your_handle",
        "username",
        "handle",
        "dogfoodhandle",
        "dogfoodxactor",
    }
)
_BLOCKED_PREFIXES: Final[tuple[str, ...]] = (
    "dogfood",
    "demouser",
    "testuser",
    "fakeuser",
    "exampleuser",
    "placeholder",
)
_PROFILE_UA: Final[str] = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
)
_OG_TITLE_RE = re.compile(
    r'property=["\']og:title["\']\s+content=["\']([^"\']*)["\']'
    r'|content=["\']([^"\']*)["\']\s+property=["\']og:title["\']',
    re.IGNORECASE,
)


class XHandleError(ValueError):
    """Raised when a user-supplied X handle must not be persisted."""


def normalize_x_handle(value: str | None) -> str | None:
    """Accept @name, name, or https://x.com/name and store the bare handle."""

    if value is None:
        return None
    cleaned = unicodedata.normalize("NFKC", value).strip()
    if not cleaned:
        return None
    lowered = cleaned.casefold()
    for prefix in (
        "https://x.com/",
        "http://x.com/",
        "https://twitter.com/",
        "http://twitter.com/",
        "https://www.x.com/",
        "https://www.twitter.com/",
    ):
        if lowered.startswith(prefix):
            cleaned = cleaned[len(prefix) :]
            break
    cleaned = cleaned.split("?")[0].split("#")[0].split("/")[0]
    cleaned = cleaned.lstrip("@").strip()
    if not cleaned or any(ch.isspace() for ch in cleaned):
        return None
    if not _HANDLE_RE.fullmatch(cleaned):
        return None
    return cleaned


def x_profile_url(handle: str | None) -> str | None:
    normalized = normalize_x_handle(handle)
    return f"https://x.com/{normalized}" if normalized else None


def is_blocked_demo_x_handle(handle: str | None) -> bool:
    """True for obvious demo/test/dogfood placeholders that must never be marked."""

    normalized = normalize_x_handle(handle)
    if normalized is None:
        return False
    key = normalized.casefold()
    if key in _BLOCKED_EXACT:
        return True
    return any(key.startswith(prefix) for prefix in _BLOCKED_PREFIXES)


def sanitize_stored_x_handle(handle: str | None) -> str | None:
    """Drop invalid or demo handles from stored data before display/persist."""

    normalized = normalize_x_handle(handle)
    if normalized is None or is_blocked_demo_x_handle(normalized):
        return None
    return normalized


def verify_x_handle_exists(
    handle: str,
    *,
    client: httpx.Client | None = None,
    timeout: float = 12.0,
) -> bool:
    """Lightweight public check that the profile page exists on x.com.

    Uses GET https://x.com/{handle} with a browser User-Agent. Missing accounts
    typically return HTTP 404 and/or an og:title containing \"Not Found\".
    """

    normalized = normalize_x_handle(handle)
    if normalized is None or is_blocked_demo_x_handle(normalized):
        return False

    owns_client = client is None
    http = client or httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": _PROFILE_UA})
    try:
        response = http.get(f"https://x.com/{normalized}", headers={"User-Agent": _PROFILE_UA})
    except httpx.HTTPError:
        return False
    finally:
        if owns_client:
            http.close()

    if response.status_code == 404:
        return False
    if response.status_code >= 400:
        return False

    og_title = _og_title(response.text)
    if og_title:
        lowered = og_title.casefold()
        if "not found" in lowered or "could not be found" in lowered:
            return False
        # Real profiles usually look like: Display Name (@handle) on X
        if f"@{normalized.casefold()}" in lowered:
            return True
        if "on x" in lowered or "on twitter" in lowered:
            return True
    # No clear positive signal — do not treat as verified.
    return False


def require_verified_x_handle(
    value: str | None,
    *,
    client: httpx.Client | None = None,
    verify: bool = True,
) -> str | None:
    """Normalize and optionally live-verify. Empty clears; failures raise XHandleError."""

    if value is None or not unicodedata.normalize("NFKC", value).strip():
        return None
    normalized = normalize_x_handle(value)
    if normalized is None:
        raise XHandleError("invalid X handle format; use @name or https://x.com/name")
    if is_blocked_demo_x_handle(normalized):
        raise XHandleError(f"X handle @{normalized} looks like a demo/test placeholder and will not be saved")
    if verify and not verify_x_handle_exists(normalized, client=client):
        raise XHandleError(
            f"X handle @{normalized} could not be verified as a real public profile; not saved"
        )
    return normalized


def _og_title(html: str) -> str | None:
    match = _OG_TITLE_RE.search(html)
    if not match:
        return None
    return match.group(1) or match.group(2)
