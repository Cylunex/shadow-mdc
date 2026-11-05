"""Anti-bot interstitial detection for provider responses.

Ported from javinizer/javinizer-go ``internal/challengedetect`` (MIT) with the
JavBus driver-verify markers from its ``scraper/javbus``. Cloudflare and site
"verify you are human" pages are frequently served with HTTP 200 (or 403/503);
parsing them yields garbage titles, so providers must fail fast with a clear
``blocked`` reason instead.

``/cdn-cgi/challenge-platform`` alone is not a wall: Cloudflare injects the
benign ``scripts/jsd`` (JavaScript Detection) asset on ordinary pages. Any
*other* URL under that prefix (orchestrate, turnstile, flow, …) is treated as
high-confidence challenge (herald pageguard hardening).
"""

from __future__ import annotations

_PLATFORM_PREFIX = "/cdn-cgi/challenge-platform"
# Characters that end a URL token in HTML or an inline script.
_URL_END = frozenset(" \t\r\n\"'<>?#&),;`")

# Non-path markers that only appear on real challenge pages.
_HIGH_CONFIDENCE_MARKERS: tuple[str, ...] = (
    "cf-browser-verification",
    "cf_chl_",
    "cf-chl-",
    "cf-challenge",
    "checking your browser before accessing",
    "enable javascript and cookies to continue",
    "ddos protection by cloudflare",
)

# Softer markers; three or more together indicate an interstitial.
_SOFT_MARKERS: tuple[str, ...] = (
    "cloudflare",
    "attention required",
    "just a moment",
    "ray id",
    "cf-ray",
    "/cdn-cgi/",
    "captcha",
    "turnstile",
)

_JAVBUS_VERIFY_MARKERS: tuple[str, ...] = (
    "/doc/driver-verify",
    "age verification javbus",
    "driver verification",
    "driver-verify?referer=",
)


def is_cloudflare_challenge(body: str) -> bool:
    """True when ``body`` looks like a Cloudflare anti-bot / interstitial page."""

    lowered = (body or "").strip().casefold()
    if not lowered:
        return False
    if _has_blocking_challenge_platform(lowered):
        return True
    if any(marker in lowered for marker in _HIGH_CONFIDENCE_MARKERS):
        return True
    return sum(1 for marker in _SOFT_MARKERS if marker in lowered) >= 3


def is_javbus_verify_page(body: str, final_url: str | None = None) -> bool:
    """True for JavBus's driver/age verification wall (served after a redirect)."""

    if final_url and "/doc/driver-verify" in final_url.casefold():
        return True
    lowered = (body or "").strip().casefold()
    return bool(lowered) and any(marker in lowered for marker in _JAVBUS_VERIFY_MARKERS)


def challenge_kind(body: str, final_url: str | None = None) -> str | None:
    """Return a short label for the challenge type, or ``None`` for normal pages."""

    if is_javbus_verify_page(body, final_url):
        return "JavBus driver-verify challenge"
    if is_cloudflare_challenge(body):
        return "Cloudflare challenge"
    return None


def _has_blocking_challenge_platform(sample: str) -> bool:
    start = 0
    while True:
        index = sample.find(_PLATFORM_PREFIX, start)
        if index < 0:
            return False
        tail = sample[index + len(_PLATFORM_PREFIX) :]
        if not _is_jsd_asset(tail):
            return True
        start = index + len(_PLATFORM_PREFIX)


def _is_jsd_asset(tail: str) -> bool:
    """True when ``tail`` is only Cloudflare's benign ``scripts/jsd`` asset.

    Accepted shapes, up to the end of the URL token:

    - ``/scripts/jsd``
    - ``/scripts/jsd/main.js`` (one filename)
    - ``/h/<id>/scripts/jsd/main.js``
    """

    rest = tail
    if rest.startswith("/h/"):
        cursor = 3
        while cursor < len(rest) and _is_host_id_char(rest[cursor]):
            cursor += 1
        if cursor == 3 or cursor >= len(rest) or rest[cursor] != "/":
            return False
        rest = rest[cursor:]
    asset = "/scripts/jsd"
    if not rest.startswith(asset):
        return False
    rest = rest[len(asset) :]
    if rest.startswith("/"):
        rest = rest[1:]
        if not rest or rest[0] in _URL_END:
            return True
        cursor = 0
        while cursor < len(rest) and _is_asset_char(rest[cursor]):
            cursor += 1
        if cursor == 0:
            return False
        rest = rest[cursor:]
    if rest.startswith("/"):
        return False
    return not rest or rest[0] in _URL_END


def _is_host_id_char(char: str) -> bool:
    return ("a" <= char <= "z") or ("0" <= char <= "9")


def _is_asset_char(char: str) -> bool:
    return _is_host_id_char(char) or char in "._-"
