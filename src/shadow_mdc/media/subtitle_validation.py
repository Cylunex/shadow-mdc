"""Conservative structural checks for downloaded SRT; no identity inference."""

import re

_MAX_BYTES = 8 * 1024 * 1024
_TIME = r"([0-9]{1,3}):([0-5][0-9]):([0-5][0-9])[,.]([0-9]{3})"
_TIMING = re.compile(rf"^{_TIME}\s+-->\s+{_TIME}(?:\s+.*)?$")
_HTML = re.compile(r"<!doctype\s+html|<html\b|<body\b|<script\b", re.IGNORECASE)


def validate_srt(payload: bytes) -> str | None:
    """Return a diagnostic for invalid structure, or None for usable cues.

    This does not prove language, edition, synchronization, or media duration.
    UTF-8, BOM-marked UTF-16 and common Chinese legacy encodings are accepted;
    original bytes are preserved by the caller.
    """
    if len(payload) > _MAX_BYTES:
        return "srt payload exceeds 8 MiB"
    encodings = (
        ("utf-16",) if payload.startswith((b"\xff\xfe", b"\xfe\xff")) else ("utf-8-sig", "gb18030", "big5")
    )
    text: str | None = None
    for encoding in encodings:
        try:
            text = payload.decode(encoding)
            break
        except UnicodeError:
            continue
    if text is None or "\x00" in text:
        return "srt payload cannot be decoded"
    if _HTML.search(text):
        return "srt payload contains an HTML page"
    previous_start = -1
    cues = 0
    for block in re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n").strip()):
        lines = block.strip().splitlines()
        if lines and lines[0].strip().isdigit():
            lines = lines[1:]
        if not lines:
            return "srt cue has no timing"
        match = _TIMING.fullmatch(lines[0].strip())
        if match is None or len(lines) < 2 or not "".join(lines[1:]).strip():
            return "srt cue has invalid timing or empty text"
        values = [int(value) for value in match.groups()]
        start = ((values[0] * 60 + values[1]) * 60 + values[2]) * 1000 + values[3]
        end = ((values[4] * 60 + values[5]) * 60 + values[6]) * 1000 + values[7]
        if end <= start or start < previous_start:
            return "srt cue has reversed or unordered timing"
        previous_start = start
        cues += 1
    return None if cues else "srt payload has no cues"
