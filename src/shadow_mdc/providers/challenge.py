"""Anti-bot interstitial detection for provider responses.

Ported from javinizer/javinizer-go ``internal/challengedetect`` (MIT) with the
JavBus driver-verify markers from its ``scraper/javbus``. Cloudflare and site
"verify you are human" pages are frequently served with HTTP 200 (or 403/503);
parsing them yields garbage titles, so providers must fail fast with a clear
``blocked`` reason instead.
"""

from __future__ import annotations

# Markers that only appear on real challenge pages. ``/cdn-cgi/challenge-platform/
# scripts/jsd/`` is injected into many *legitimate* Cloudflare-fronted pages, so
# only the orchestration endpoints count.
_HIGH_CONFIDENCE_MARKERS: tuple[str, ...] = (
    "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page/v1",
    "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_hr/v1",
    "/cdn-cgi/challenge-platform/h/b/orchestrate/chl_page",
    "/cdn-cgi/challenge-platform/h/g/orchestrate/chl_page",
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
