import re
import unicodedata
from pathlib import Path

from .domain import IdentityHints
from .enums import ContentFamily, QueryMode

_NOISE = re.compile(
    r"(?ix)(?:^|[._\-\s])("
    r"2160p|1080p|720p|4k|8k|uhd|fhd|bluray|web[-_. ]?dl|hevc|"
    r"h[._-]?26[45]|x26[45]|av1|aac|uncensored|leaked|chinese|subtitle|中文字幕|中字|字幕|中文"
    r")(?=$|[._\-\s])"
)
_DOMAIN = re.compile(r"(?i)(?:www\.)?[a-z0-9-]+\.(?:com|net|org|tv|cc|me|xyz|top)")
_BRACKETS = re.compile(r"[\[【(\uFF08].*?[\]】)\uFF09]")
_SEPARATORS = re.compile(r"[._\s]+")

_FC2 = re.compile(r"(?i)\bFC2(?:[-_. ]?PPV)?[-_. ]?(\d{5,9})\b")
_HEYZO = re.compile(r"(?i)\bHEYZO[-_. ]?(\d{3,5})\b")
_CHINESE = re.compile(r"(?i)\b((?:MD|MDSR|MDWP|MDCM|MKY-[A-Z]+)[-_]?[A-Z]*\d{3,6})\b")
_WESTERN_DATE = re.compile(r"(?i)\b([a-z][a-z0-9-]{1,30})[._ -](?:20)?(\d{2})[._ -](\d{2})[._ -](\d{2})\b")
_JAV = re.compile(r"(?i)(?<![A-Z0-9])((?:\d{2,5})?[A-Z]{2,10})[-_. ]?(\d{2,6})(?![A-Z0-9])")
_UNCENSORED = re.compile(r"(?i)\b(1PONDO|CARIB|CARIBPR|10MUSUME|PACOPACOMAMA)[-_ ]?(\d{6})[-_ ](\d{2,4})\b")

_DISALLOWED_PREFIXES = frozenset(
    {"H264", "H265", "X264", "X265", "HEVC", "AVC", "AAC", "MP4", "MKV", "WEB", "FHD", "UHD"}
)


def clean_stem(path_or_name: str | Path) -> str:
    stem = Path(path_or_name).stem
    normalized = unicodedata.normalize("NFKC", stem)
    normalized = _DOMAIN.sub(" ", normalized)
    normalized = _BRACKETS.sub(" ", normalized)
    normalized = _NOISE.sub(" ", normalized)
    normalized = _SEPARATORS.sub(" ", normalized)
    return " ".join(normalized.split()).strip(" -_.")


def extract_code(text: str) -> tuple[str | None, ContentFamily]:
    normalized = unicodedata.normalize("NFKC", text)

    if match := _FC2.search(normalized):
        return f"FC2-{match.group(1)}", ContentFamily.JAV
    if match := _HEYZO.search(normalized):
        return f"HEYZO-{match.group(1)}", ContentFamily.JAV
    if match := _UNCENSORED.search(normalized):
        return f"{match.group(1).upper()}-{match.group(2)}-{match.group(3)}", ContentFamily.JAV
    if match := _CHINESE.search(normalized):
        value = re.sub(r"(?<=[A-Z])(?=\d)", "-", match.group(1).upper().replace("_", "-"))
        return value.replace("--", "-"), ContentFamily.CHINESE
    if match := _WESTERN_DATE.search(normalized):
        studio, year, month, day = match.groups()
        return f"{studio.lower()}.{year}.{month}.{day}", ContentFamily.WESTERN
    if match := _JAV.search(normalized):
        prefix, digits = match.groups()
        prefix = prefix.upper()
        if prefix in _DISALLOWED_PREFIXES:
            return None, ContentFamily.UNKNOWN
        return f"{prefix}-{digits}", ContentFamily.JAV
    return None, ContentFamily.UNKNOWN


def build_identity_hints(
    path_or_name: str | Path,
    *,
    source_url: str | None = None,
    external_ids: dict[str, str] | None = None,
    fingerprints: dict[str, str] | None = None,
    duration_seconds: float | None = None,
) -> IdentityHints:
    clean = clean_stem(path_or_name)
    code, family = extract_code(clean)
    known_ids = external_ids or {}
    known_fingerprints = fingerprints or {}

    if source_url:
        mode = QueryMode.URL
        term = source_url
    elif known_ids:
        mode = QueryMode.EXTERNAL_ID
        term = next(iter(known_ids.values()))
    elif code:
        mode = QueryMode.CODE
        term = code
    elif known_fingerprints:
        mode = QueryMode.FINGERPRINT
        term = next(iter(known_fingerprints.values()))
    else:
        mode = QueryMode.TEXT
        term = clean

    return IdentityHints(
        term=term,
        mode=mode,
        family=family,
        code=code,
        title=clean if clean != code else None,
        source_url=source_url,
        external_ids=known_ids,
        fingerprints=known_fingerprints,
        duration_seconds=duration_seconds,
        file_path=str(path_or_name),
    )


def normalize_identity_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)
