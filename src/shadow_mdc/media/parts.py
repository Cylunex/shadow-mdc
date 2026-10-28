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


# Strip only recognized language/accessibility tags, never release names or part numbers.
_SUBTITLE_TAGS = re.compile(
    r"(?i)(?:[. _-](?:zh(?:[-_](?:cn|tw|hans|hant))?|chs|cht|chi|zho|"
    r"en|eng|ja|jpn|jp|ko|kor|fr|fra|fre|de|deu|ger|es|spa|it|ita|"
    r"pt(?:[-_]br)?|por|ru|rus|ar|ara|th|tha|vi|vie|id|ind|"
    r"forced|sdh|cc|default|简体|繁体|简中|繁中|中文|双语))+$"
)


def _subtitle_name(subtitle: Path) -> tuple[str, str]:
    match = _SUBTITLE_TAGS.search(subtitle.stem)
    if match is None:
        return subtitle.stem, ""
    return subtitle.stem[:match.start()], match.group()


def subtitle_matches_stem(subtitle: Path, stem: str) -> bool:
    """Whether a sidecar names this media stem plus recognized subtitle tags."""
    return _exact_subtitle_tail(subtitle, stem) is not None


def _exact_subtitle_tail(subtitle: Path, stem: str) -> str | None:
    if _fold_name(subtitle.stem) == _fold_name(stem):
        return ""
    for separator in re.finditer(r"[. _-]", subtitle.stem):
        base, tail = subtitle.stem[:separator.start()], subtitle.stem[separator.start():]
        if _fold_name(base) == _fold_name(stem) and _SUBTITLE_TAGS.fullmatch(tail):
            return tail
    return None


def _fold_name(value: str) -> str:
    return unicodedata.normalize("NFC", value).casefold()


def _code_pattern(code: str) -> str:
    # Accept delimiter differences, but never a prefix of a longer code.
    return r"[-_. ]*".join(re.escape(piece) for piece in re.split(r"[-_. ]+", code))


def _code_part(base: str, code: str) -> int | None:
    match = re.fullmatch(
        _code_pattern(code) + r"(?:[-_. ]+(?:cd|part|pt|v)?0*([1-9]\d*)|[-_. ]+([a-i]))?",
        base,
        re.IGNORECASE,
    )
    if match is None:
        return None
    if match.group(1):
        return int(match.group(1))
    if match.group(2):
        return ord(match.group(2).upper()) - ord("A") + 1
    return 0


def find_subtitles(media: Path, code: str | None) -> tuple[Path, ...]:
    """Pair sidecars with one local asset; ambiguous versions stay untouched.

    Exact media basenames take precedence. Code-only fallback requires the same
    explicit part and one eligible sibling. STRM is treated as a local filename;
    its locator is never opened here. Subtitle symlinks are not followed.
    """
    try:
        siblings = sorted(media.parent.iterdir(), key=lambda item: item.name.casefold())
        videos = [
            item for item in siblings
            if item.suffix.casefold() in _VIDEO_NAME_SUFFIXES and item.is_file()
        ]
        # Also supports callers checking a target before the media exists.
        if media not in videos:
            videos.append(media)
        part = detect_media_part(media, code)
        matches: list[Path] = []
        for candidate in siblings:
            if (
                candidate.suffix.casefold() not in SUBTITLE_EXTENSIONS
                or candidate.is_symlink()
                or not candidate.is_file()
            ):
                continue
            base, _tail = _subtitle_name(candidate)
            exact = [item for item in videos if subtitle_matches_stem(candidate, item.stem)]
            if exact:
                longest = max(len(_fold_name(item.stem)) for item in exact)
                exact = [item for item in exact if len(_fold_name(item.stem)) == longest]
                if exact == [media]:
                    matches.append(candidate)
                continue
            if code is None:
                continue
            subtitle_part = _code_part(base, code)
            if subtitle_part is None or subtitle_part != (part.index if part else 0):
                continue
            peers = []
            for video in videos:
                peer_part = detect_media_part(video, code)
                if (peer_part.index if peer_part else 0) == subtitle_part:
                    peers.append(video)
            if peers == [media]:
                matches.append(candidate)
        return tuple(matches)
    except OSError:
        return ()


def subtitle_destination(subtitle: Path, media: Path, destination: Path) -> Path:
    """Preserve language/forced/SDH tags even when paired by code or part."""
    tail = _exact_subtitle_tail(subtitle, media.stem)
    if tail is None:
        _base, tail = _subtitle_name(subtitle)
    if tail and tail[0] not in ".-_":
        tail = f".{tail.lstrip()}"
    return destination.with_name(f"{destination.stem}{tail}{subtitle.suffix.casefold()}")


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
