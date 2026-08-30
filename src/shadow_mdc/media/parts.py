import re
from dataclasses import dataclass
from pathlib import Path

SUBTITLE_EXTENSIONS = frozenset({".srt", ".ass", ".ssa", ".vtt", ".sub"})

_EXPLICIT_PART = re.compile(r"(?i)(?:^|[-_. ])(?:cd|part|pt)0*(?P<number>[1-9]\d*)$")
_VERSION_PART = re.compile(r"(?i)(?:^|[-_. ])v0*(?P<number>[1-9]\d*)$")
_LETTER_PART = re.compile(r"(?i)(?:^|[-_. ])(?P<letter>[a-i])$")
_TRAILING_PART = re.compile(r"(?:^|[-_. ])(?P<number>[1-9])$")


@dataclass(frozen=True, slots=True)
class MediaPart:
    index: int

    @property
    def suffix(self) -> str:
        return f"-CD{self.index}"


def detect_media_part(path: Path, code: str | None) -> MediaPart | None:
    stem = path.stem.strip()
    match = _EXPLICIT_PART.search(stem)
    if match:
        return MediaPart(index=int(match.group("number")))

    parent_match = _EXPLICIT_PART.fullmatch(path.parent.name.strip())
    if parent_match:
        return MediaPart(index=int(parent_match.group("number")))

    if code is None:
        return None
    match = _VERSION_PART.search(stem) or _TRAILING_PART.search(stem)
    if match:
        return MediaPart(index=int(match.group("number")))
    letter = _LETTER_PART.search(stem)
    if letter:
        return MediaPart(index=ord(letter.group("letter").upper()) - ord("A") + 1)
    return None


def find_subtitles(media: Path, code: str | None) -> tuple[Path, ...]:
    media_part = detect_media_part(media, code)
    matches: list[Path] = []
    try:
        siblings = sorted(media.parent.iterdir(), key=lambda item: item.name.casefold())
    except OSError:
        return ()
    for candidate in siblings:
        if not candidate.is_file() or candidate.suffix.casefold() not in SUBTITLE_EXTENSIONS:
            continue
        if _subtitle_matches(candidate, media, code, media_part):
            matches.append(candidate)
    return tuple(matches)


def subtitle_destination(subtitle: Path, media: Path, destination: Path) -> Path:
    tail = ""
    if subtitle.stem.casefold().startswith(media.stem.casefold()):
        tail = subtitle.stem[len(media.stem) :]
    if tail and tail[0] not in ".-_":
        tail = f".{tail}"
    return destination.with_name(f"{destination.stem}{tail}{subtitle.suffix.casefold()}")


def _subtitle_matches(
    subtitle: Path,
    media: Path,
    code: str | None,
    media_part: MediaPart | None,
) -> bool:
    if subtitle.stem.casefold().startswith(media.stem.casefold()):
        return True
    if code is None or _compact(code) not in _compact(subtitle.stem):
        return False
    subtitle_part = detect_media_part(subtitle, code)
    if media_part is None:
        return subtitle_part is None
    if subtitle_part is None:
        return media_part.index == 1
    return subtitle_part.index == media_part.index


def _compact(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.casefold())
