import re
from pathlib import Path, PureWindowsPath
from urllib.parse import urlsplit, urlunsplit

MAX_STRM_BYTES = 64 * 1024
MAX_LOCATOR_LENGTH = 8192
_WINDOWS_ABSOLUTE = re.compile(r"(?i)^[a-z]:[\\/]")
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_BLOCKED_SCHEMES = frozenset({"data", "javascript"})


def read_strm_locator(path: str | Path) -> str:
    strm_path = Path(path)
    size = strm_path.stat().st_size
    if size > MAX_STRM_BYTES:
        raise ValueError(f"STRM file exceeds {MAX_STRM_BYTES} bytes")
    text = _decode(strm_path.read_bytes())
    locator = next(
        (
            line.strip().strip("\"'")
            for line in text.splitlines()
            if line.strip() and not line.lstrip().startswith("#")
        ),
        "",
    )
    if not locator:
        raise ValueError("STRM file has no media locator")
    if len(locator) > MAX_LOCATOR_LENGTH:
        raise ValueError(f"STRM media locator exceeds {MAX_LOCATOR_LENGTH} characters")
    if _CONTROL.search(locator):
        raise ValueError("STRM media locator contains control characters")
    return _normalize_locator(locator, strm_path.parent)


def write_strm(path: str | Path, locator: str) -> Path:
    """Write a single-line UTF-8 .strm file (no BOM). Creates parent dirs."""

    text = locator.strip()
    if not text:
        raise ValueError("STRM media locator is empty")
    if len(text) > MAX_LOCATOR_LENGTH:
        raise ValueError(f"STRM media locator exceeds {MAX_LOCATOR_LENGTH} characters")
    if _CONTROL.search(text):
        raise ValueError("STRM media locator contains control characters")
    if "\n" in text or "\r" in text:
        raise ValueError("STRM media locator must be a single line")
    parsed = urlsplit(text)
    if parsed.scheme and parsed.scheme.casefold() in _BLOCKED_SCHEMES:
        raise ValueError(f"STRM URI scheme is not allowed: {parsed.scheme}")

    strm_path = Path(path)
    strm_path.parent.mkdir(parents=True, exist_ok=True)
    temporary = strm_path.with_suffix(strm_path.suffix + ".tmp")
    payload = (text + "\n").encode("utf-8")
    if len(payload) > MAX_STRM_BYTES:
        raise ValueError(f"STRM file exceeds {MAX_STRM_BYTES} bytes")
    temporary.write_bytes(payload)
    temporary.replace(strm_path)
    return strm_path


def redact_media_locator(locator: str) -> str:
    """Remove credentials, query tokens, and fragments before persisting a URI as metadata."""

    parsed = urlsplit(locator)
    if not parsed.scheme:
        return locator
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    return urlunsplit((parsed.scheme, netloc, parsed.path, "", ""))


def _decode(raw: bytes) -> str:
    if raw.startswith((b"\xff\xfe", b"\xfe\xff")):
        try:
            return raw.decode("utf-16")
        except UnicodeError:
            pass
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding)
        except UnicodeError:
            continue
    raise ValueError("STRM file is not valid UTF-8, UTF-16, or GB18030 text")


def _normalize_locator(locator: str, parent: Path) -> str:
    if locator.startswith("\\\\") or _WINDOWS_ABSOLUTE.match(locator):
        return str(PureWindowsPath(locator))
    parsed = urlsplit(locator)
    if parsed.scheme:
        if parsed.scheme.casefold() in _BLOCKED_SCHEMES:
            raise ValueError(f"STRM URI scheme is not allowed: {parsed.scheme}")
        return locator
    target = Path(locator)
    if target.is_absolute():
        return str(target)
    return str((parent / target).resolve())
