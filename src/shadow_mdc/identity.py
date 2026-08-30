import re
import unicodedata
from pathlib import Path
from urllib.parse import unquote, urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .classification import classify_media
from .domain import IdentityHints
from .enums import ContentFamily, MediaCategory, QueryMode

_NOISE = re.compile(
    r"(?ix)(?:^|[._\-\s])("
    r"2160p|1080p|720p|4k|8k|uhd|fhd|bluray|web[-_. ]?dl|hevc|"
    r"h[._-]?26[45]|x26[45]|av1|aac|uncensored|leaked|chinese|subtitle|中文字幕|中字|字幕|中文"
    r")(?=$|[._\-\s])"
)
_DOMAIN = re.compile(r"(?i)(?:www\.)?[a-z0-9-]+\.(?:com|net|org|tv|cc|me|xyz|top|vip)")
_BRACKETS = re.compile(r"[\[【(\uFF08].*?[\]】)\uFF09]")
_SEPARATORS = re.compile(r"[._\s]+")
_GENERIC_STEM = re.compile(
    r"(?ix)^(?:\d{1,4}|cd\s*\d+|disc\s*\d+|part\s*\d+|ep\s*\d+|"
    r"v\s*\d+\s*[-~\uFF5E至到]\s*\d+|[vp]\s*(?:\d+|[\(\uFF08]\d+[\)\uFF09])?|"
    r"video|movie|影片|视频|完整(?:版)?|full)$"
)

_FC2 = re.compile(r"(?i)\bFC2(?:[-_. ]?PPV)?[-_. ]?(\d{5,9})\b")
_HEYZO = re.compile(r"(?i)\bHEYZO[-_. ]?(\d{3,5})\b")
_CHINESE = re.compile(r"(?i)\b((?:MD|MDSR|MDWP|MDCM|MKY-[A-Z]+)[-_]?[A-Z]*\d{3,6})\b")
_WESTERN_DATE = re.compile(r"(?i)\b([a-z][a-z0-9-]{1,30})[._ -](?:20)?(\d{2})[._ -](\d{2})[._ -](\d{2})\b")
_JAV = re.compile(
    r"(?i)(?<![A-Z0-9])((?:\d{2,5})?[A-Z]{2,10})([-_. ]?)(\d{2,6})"
    r"(?:[-_. ]?(?:CD|DISC|PART)?[A-D])?(?![A-Z0-9])"
)
_UNCENSORED = re.compile(r"(?i)\b(1PONDO|CARIB|CARIBPR|10MUSUME|PACOPACOMAMA)[-_ ]?(\d{6})[-_ ](\d{2,4})\b")

_DISALLOWED_PREFIXES = frozenset(
    {
        "AAC",
        "AVC",
        "FHD",
        "H264",
        "H265",
        "HEVC",
        "IMG",
        "MANYVIDS",
        "MKV",
        "MOV",
        "MP4",
        "ONLYFANS",
        "UHD",
        "WEB",
        "X264",
        "X265",
    }
)
_KNOWN_JAV_PREFIXES = frozenset(
    {
        "336KNB",
        "ABF",
        "ABP",
        "ABW",
        "ACHJ",
        "ADN",
        "APAA",
        "ATID",
        "AVAV",
        "AVOP",
        "BBAN",
        "BF",
        "BHD",
        "CAWD",
        "CJOD",
        "DASD",
        "DASS",
        "DEAB",
        "DLDSS",
        "DVMM",
        "DVRT",
        "EBOD",
        "EBWH",
        "FAYS",
        "FFT",
        "FJIN",
        "FKOU",
        "FNEO",
        "FNS",
        "FSDSS",
        "GEBB",
        "GUPP",
        "GVG",
        "GVH",
        "HAWA",
        "HMN",
        "HND",
        "IPX",
        "IPZ",
        "IPZZ",
        "JUFE",
        "JUL",
        "JUQ",
        "JUR",
        "JUX",
        "JUY",
        "KATU",
        "KCKC",
        "KNAM",
        "LT",
        "LULU",
        "MANX",
        "MDB",
        "MDYD",
        "MEYD",
        "MFYD",
        "MIAA",
        "MIDA",
        "MIDE",
        "MIDV",
        "MIKR",
        "MIMK",
        "MIRD",
        "MKMP",
        "MVSD",
        "NACR",
        "NACT",
        "NCYF",
        "NHDTB",
        "NIMA",
        "NMSL",
        "NNPJ",
        "NTRH",
        "OAE",
        "OFES",
        "PASN",
        "PFES",
        "PPPD",
        "PPPE",
        "PRED",
        "RCT",
        "RCTD",
        "REAL",
        "ROYD",
        "SABA",
        "SAME",
        "SDDE",
        "SDMS",
        "SGKI",
        "SNIS",
        "SNOS",
        "SOGO",
        "SONE",
        "SPSE",
        "SSIS",
        "SSNI",
        "STARS",
        "START",
        "SVVRT",
        "TEK",
        "TIKB",
        "TOP",
        "TYSF",
        "URE",
        "US",
        "UUE",
        "UUR",
        "UUV",
        "WAAA",
        "WANZ",
        "WSA",
        "YMDD",
        "YMDS",
        "YUJ",
        "ZEX",
        "ZUKO",
    }
)


class IdentityAliasRules(BaseModel):
    """User-maintained aliases used as hints, never as stable identities."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    studios: dict[str, str] = Field(default_factory=dict)
    series: dict[str, str] = Field(default_factory=dict)
    actors: dict[str, str] = Field(default_factory=dict)

    @field_validator("studios", "series", "actors")
    @classmethod
    def validate_alias_map(cls, value: dict[str, str]) -> dict[str, str]:
        if any(not alias.strip() or not canonical.strip() for alias, canonical in value.items()):
            raise ValueError("alias keys and canonical values must not be blank")
        return value


class AliasMatch(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    studio: str | None = None
    series: str | None = None
    actors: tuple[str, ...] = ()
    evidence: tuple[str, ...] = ()


def clean_stem(path_or_name: str | Path) -> str:
    stem = Path(path_or_name).stem
    normalized = unicodedata.normalize("NFKC", stem)
    normalized = _DOMAIN.sub(" ", normalized)
    normalized = _BRACKETS.sub(" ", normalized)
    normalized = _NOISE.sub(" ", normalized)
    normalized = _SEPARATORS.sub(" ", normalized)
    return " ".join(normalized.split()).strip(" -_.")


def extract_code(
    text: str,
    category: MediaCategory = MediaCategory.OTHER,
) -> tuple[str | None, ContentFamily]:
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
        prefix, separator, digits = match.groups()
        prefix = prefix.upper()
        if prefix in _DISALLOWED_PREFIXES or (not separator and len(prefix) > 6):
            return None, ContentFamily.UNKNOWN
        if (
            category
            in {
                MediaCategory.CHINA,
                MediaCategory.KOREA,
                MediaCategory.EUROPE,
            }
            and prefix not in _KNOWN_JAV_PREFIXES
        ):
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
    media_locator: str | None = None,
    context_names: tuple[str, ...] = (),
    alias_rules: IdentityAliasRules | None = None,
    category: MediaCategory = MediaCategory.OTHER,
) -> IdentityHints:
    clean = clean_stem(path_or_name)
    context = tuple(value for value in (clean, *context_names) if value)
    locator_name = _locator_name(media_locator)
    locator_context = clean_stem(locator_name) if locator_name else ""
    preliminary = classify_media(*context, locator_context, fallback=category)
    code, family = _first_code((*context, locator_context), preliminary.category)
    title = _select_title(clean, context_names, locator_name, code)
    alias_match = match_aliases(" ".join((*context, locator_context)), alias_rules)
    classification = classify_media(
        *context,
        locator_context,
        detected_family=family,
        fallback=preliminary.category,
    )
    family = classification.family
    category = classification.category
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
        term = title

    return IdentityHints(
        term=term,
        mode=mode,
        family=family,
        category=category,
        code=code,
        title=title if title != code else None,
        source_url=source_url,
        external_ids=known_ids,
        fingerprints=known_fingerprints,
        duration_seconds=duration_seconds,
        file_path=str(path_or_name),
        media_locator=media_locator,
        studio=alias_match.studio,
        series=alias_match.series,
        actors=alias_match.actors,
        alias_evidence=alias_match.evidence,
    )


def match_aliases(text: str, rules: IdentityAliasRules | None) -> AliasMatch:
    if rules is None:
        return AliasMatch()
    normalized = unicodedata.normalize("NFKC", text).casefold()
    evidence: list[str] = []

    studio = _first_alias(normalized, rules.studios)
    if studio is not None:
        evidence.append(f"studio:{studio}")
    series = _first_alias(normalized, rules.series)
    if series is not None:
        evidence.append(f"series:{series}")
    actors = _all_aliases(normalized, rules.actors)
    evidence.extend(f"actor:{actor}" for actor in actors)
    return AliasMatch(studio=studio, series=series, actors=actors, evidence=tuple(evidence))


def _first_alias(text: str, aliases: dict[str, str]) -> str | None:
    matches = _all_aliases(text, aliases)
    return matches[0] if matches else None


def _all_aliases(text: str, aliases: dict[str, str]) -> tuple[str, ...]:
    matched: list[tuple[int, str]] = []
    for alias, canonical in aliases.items():
        normalized_alias = unicodedata.normalize("NFKC", alias).casefold().strip()
        if normalized_alias and _contains_alias(text, normalized_alias):
            matched.append((len(normalized_alias), canonical.strip()))
    unique: list[str] = []
    for _, canonical in sorted(matched, key=lambda item: (-item[0], item[1].casefold())):
        if canonical and canonical not in unique:
            unique.append(canonical)
    return tuple(unique)


def _contains_alias(text: str, alias: str) -> bool:
    if alias.isascii() and re.fullmatch(r"[a-z0-9 ]+", alias):
        return re.search(rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])", text) is not None
    return alias in text


def _first_code(
    values: tuple[str, ...],
    category: MediaCategory,
) -> tuple[str | None, ContentFamily]:
    for value in values:
        if not value:
            continue
        code, family = extract_code(value, category)
        if code:
            return code, family
    return None, ContentFamily.UNKNOWN


def _select_title(
    clean: str,
    context_names: tuple[str, ...],
    locator_name: str,
    code: str | None,
) -> str:
    candidates = (clean, locator_name, *context_names)
    for candidate in candidates:
        value = clean_stem(candidate)
        if value and value != code and not _GENERIC_STEM.fullmatch(value):
            return value
    return code or clean or next((value for value in candidates if value), "未命名影片")


def _locator_name(locator: str | None) -> str:
    if not locator:
        return ""
    parsed = urlsplit(locator)
    if parsed.scheme and parsed.path:
        return unquote(parsed.path.rstrip("/").rsplit("/", 1)[-1])
    return Path(locator).name


def normalize_identity_value(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).casefold()
    return re.sub(r"[^a-z0-9]+", "", normalized)
