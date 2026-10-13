import re
import unicodedata
from collections.abc import Sequence
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


def part_group_key(path: Path, code: str | None) -> str:
    """Stable key so CD1/CD2 (and lettered parts) collapse to one Work group."""

    if code:
        return f"code:{_compact(code)}"
    stem = path.stem.strip()
    for pattern in (_EXPLICIT_PART, _VERSION_PART, _LETTER_PART, _TRAILING_PART):
        match = pattern.search(stem)
        if match:
            stem = stem[: match.start()].rstrip("-_. ")
            break
    return f"stem:{_compact(stem) or path.name.casefold()}"


# --- Directory letter-volume merge (miyabi-inspired; ideas only / no GPL copy) ---

_GENERIC_SIDECAR_STEMS = frozenset(
    {
        "movie",
        "tvshow",
        "album",
        "artist",
        "extrafanart",
        "poster",
        "fanart",
        "landscape",
        "folder",
    }
)
_VIDEO_NAME_SUFFIXES = frozenset(
    {
        ".3gp",
        ".avi",
        ".f4v",
        ".flv",
        ".iso",
        ".m2ts",
        ".m4v",
        ".mkv",
        ".mov",
        ".mp4",
        ".mpeg",
        ".mpg",
        ".rm",
        ".rmvb",
        ".ts",
        ".vob",
        ".webm",
        ".wmv",
        ".strm",
    }
)


@dataclass(frozen=True, slots=True)
class LetterVolume:
    """One lettered CD part: exact stem prefix (incl. separators) + A-Z letter."""

    prefix: str
    letter: str
    base: str

    @property
    def index(self) -> int:
        return ord(self.letter) - ord("A") + 1


def parse_letter_volume(stem: str) -> LetterVolume | None:
    """Parse ``…A`` / ``…-B`` / bare ``C`` letter volume markers from a file stem."""

    name = unicodedata.normalize("NFKC", stem).strip().upper()
    if len(name) < 1:
        return None
    letter = name[-1]
    if letter < "A" or letter > "Z":
        return None
    prefix = name[:-1]
    base = prefix.rstrip("-_. ")
    return LetterVolume(prefix=prefix, letter=letter, base=base)



def _directory_catalogue_code(directory_name: str) -> str | None:
    """Basename must itself be a catalogue code (format-tolerant), not merely contain one."""

    from ..identity import extract_code
    from ..normalize_code import is_format_equivalent, normalize_code

    code, _family = extract_code(directory_name)
    if code and is_format_equivalent(normalize_code(directory_name) or directory_name, code):
        return code
    # Bare uncensored date folders (060326_001) are valid directory identities.
    match = re.fullmatch(r"(\d{6})[-_](\d{2,4})", unicodedata.normalize("NFKC", directory_name).strip())
    if match:
        return f"{match.group(1)}-{match.group(2)}"
    return None


def directory_volume_merge_code(
    directory: str | Path,
    video_names: Sequence[str],
    sidecar_names: Sequence[str] = (),
    *,
    sizes: Sequence[int | None] | None = None,
    min_video_bytes: int = 0,
) -> str | None:
    """Return the directory 番号 when videos form a contiguous letter-volume set.

    Requires:
    * directory basename is itself a valid catalogue code (not merely containing one)
    * ≥2 eligible videos share one exact stem prefix + letters A,B,… with no gaps/dupes
    * each non-empty stem base is format-equivalent to the directory code (padding OK)
    * bare letters (``a.mp4`` / ``B.mkv``) are allowed
    * conflicting / per-part sidecars veto the whole group
    Undersized files (when ``min_video_bytes`` > 0) are ignored as trailers.
    """

    from ..normalize_code import is_format_equivalent

    directory_name = Path(directory).name.strip()
    if not directory_name:
        return None
    code = _directory_catalogue_code(directory_name)
    if not code:
        return None

    if sizes is not None and len(sizes) != len(video_names):
        raise ValueError("sizes must align with video_names")

    stem: str | None = None
    parts: dict[str, bool] = {}
    for index, raw_name in enumerate(video_names):
        size = sizes[index] if sizes is not None else None
        if min_video_bytes > 0 and size is not None and size < min_video_bytes:
            continue
        name = Path(raw_name).name
        suffix = Path(name).suffix.casefold()
        file_stem = Path(name).stem if suffix in _VIDEO_NAME_SUFFIXES or suffix == "" else name
        volume = parse_letter_volume(file_stem)
        if volume is None:
            return None
        if volume.base and not is_format_equivalent(volume.base, code):
            return None
        if volume.letter in parts:
            return None
        if stem is None:
            stem = volume.prefix
        elif volume.prefix != stem:
            return None
        parts[volume.letter] = True

    if len(parts) < 2:
        return None
    for offset in range(len(parts)):
        expected = chr(ord("A") + offset)
        if expected not in parts:
            return None

    for sidecar in sidecar_names:
        if _sidecar_conflicts_volume_merge(code, sidecar):
            return None
    return code


def _sidecar_conflicts_volume_merge(directory_code: str, sidecar_name: str) -> bool:
    from ..identity import extract_code
    from ..normalize_code import is_format_equivalent

    stem = Path(sidecar_name).stem.strip()
    if not stem or stem.casefold() in _GENERIC_SIDECAR_STEMS:
        return False
    volume = parse_letter_volume(stem)
    if volume is not None and volume.base and is_format_equivalent(volume.base, directory_code):
        # Per-part sidecar (e.g. FJIN-106A.nfo) outweighs the directory convention.
        return True
    other, _family = extract_code(stem)
    return bool(other and not is_format_equivalent(other, directory_code))


def volume_merge_identity_blocks(directory_code: str, existing_code: str | None, *, confirmed: bool) -> bool:
    """True when a confirmed manual/scraped identity vetoes directory volume merge."""

    from ..normalize_code import is_format_equivalent

    if not confirmed or not existing_code:
        return False
    return not is_format_equivalent(existing_code, directory_code)
