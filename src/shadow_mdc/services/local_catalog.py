import hashlib
import re
import unicodedata
from pathlib import Path

from ..classification import classify_media
from ..domain import IdentityHints, ProviderRecord
from ..enums import ContentFamily, MediaCategory
from ..identity import clean_stem, normalize_identity_value

_HANDLE = re.compile(r"@([A-Za-z][A-Za-z0-9_]{1,31})")
_BRACKETED = re.compile(r"[【「『\[]([^】」』\]]{1,40})[】」』\]]")
_HASHTAG = re.compile(r"#([A-Za-z][A-Za-z0-9_]{1,31}|[\u3400-\u9fff]{2,12})")
_VIDEO_RANGE_PART = re.compile(r"(?i)^v\s*[-_. ]*(\d+)\s*[-~\uFF5E至到]\s*(\d+)$")
_VIDEO_PART = re.compile(r"(?i)^v(?:\s*[\(\uFF08]\s*(\d+)\s*[\)\uFF09]|\s*[-_. ]*(\d+))?$")
_PREFIXED_PART = re.compile(r"(?i)^([pc])\s*(?:[\(\uFF08]\s*(\d+)\s*[\)\uFF09]|[-_. ]*(\d+))$")
_NUMBERED_COPY = re.compile(r"^(\d+)\s*[\(\uFF08]\s*(\d+)\s*[\)\uFF09]$")
_PLAIN_NUMBER = re.compile(r"^\d{1,4}$")
_GENERIC_WORD_PART = re.compile(
    r"(?i)^(?:video|movie|part|episode|ep|cd|disc|影片|视频|視頻)\s*[-_. ]*(\d+)?$"
)
_OPAQUE_FILE = re.compile(r"(?i)^(?:lv[_ .-]|\d+[_ .-]\d{8,})")
_SITE_PREFIX = re.compile(r"(?i)^(?:www\.)?[a-z0-9-]+\.(?:com|net|org|tv|cc|me|xyz|top|vip)[@_-]+")

_TAXONOMY_NAMES = frozenset(
    {
        "japan",
        "jav",
        "有码",
        "有码破解",
        "无码",
        "日本",
        "china",
        "国产",
        "國產",
        "自拍",
        "探花",
        "伪娘",
        "偽娘",
        "媚黑",
        "korea",
        "韩国",
        "韓國",
        "europe",
        "欧美",
        "歐美",
        "western",
        "other",
        "其他",
        "v",
        "video",
        "影片",
    }
)


def infer_media_category(*values: str) -> MediaCategory:
    return classify_media(*values).category


def family_for_category(category: MediaCategory) -> ContentFamily:
    return {
        MediaCategory.JAPAN: ContentFamily.JAV,
        MediaCategory.CHINA: ContentFamily.CHINESE,
        MediaCategory.KOREA: ContentFamily.KOREAN,
        MediaCategory.EUROPE: ContentFamily.WESTERN,
        MediaCategory.OTHER: ContentFamily.UNKNOWN,
    }[category]


def local_context_names(path: Path, root: Path) -> tuple[str, ...]:
    """Return nearby directory names from nearest to farthest for local inference."""

    try:
        relative_parent = path.parent.relative_to(root)
    except ValueError:
        return ()
    return tuple(reversed(relative_parent.parts[-3:]))


def is_video_part_name(raw_stem: str) -> bool:
    normalized = unicodedata.normalize("NFKC", _remove_site_prefix(raw_stem)).strip()
    return bool(_VIDEO_RANGE_PART.fullmatch(normalized) or _VIDEO_PART.fullmatch(normalized))


def is_generic_file_name(raw_stem: str) -> bool:
    """Return whether a filename is only a part/index/opaque local identifier."""

    return _is_generic_file_name(raw_stem)


def build_local_catalog_record(
    *,
    library_id: str,
    root: Path,
    path: Path,
    hints: IdentityHints,
    actor_directory: Path | None = None,
) -> ProviderRecord:
    category = hints.category
    relative = _relative_path(path, root)
    subject = _nearest_subject(path, root, hints.code)
    hierarchical = _hierarchical_local_metadata(
        path=path,
        root=root,
        actor_directory=actor_directory,
        actor=next(iter(hints.actors), None),
    )
    title = hierarchical[0] or _local_title(path.stem, hints, subject)
    family = hints.family if hints.family is not ContentFamily.UNKNOWN else family_for_category(category)
    external_id = hashlib.sha256(f"{library_id}\0{relative.as_posix().casefold()}".encode()).hexdigest()
    confirmed_directory_actor = "directory-actor:confirmed" in hints.alias_evidence
    actors = (
        hints.actors
        if confirmed_directory_actor and hints.actors
        else (
            (subject,)
            if subject and _is_generic_file_name(path.stem)
            else hints.actors or ((subject,) if subject else ())
        )
    )
    series = hints.series
    if series is None and _is_generic_file_name(path.stem):
        series = actors[0] if confirmed_directory_actor and actors else subject
    return ProviderRecord(
        provider="local-path",
        external_id=external_id,
        code=hints.code,
        title=title,
        family=family,
        category=category,
        studio=hints.studio,
        series=series,
        actors=actors,
        tags=tuple(dict.fromkeys((category.value, *hierarchical[1]))),
        fingerprints=hints.fingerprints,
        language=_language_for_category(category),
    )


def _hierarchical_local_metadata(
    *,
    path: Path,
    root: Path,
    actor_directory: Path | None,
    actor: str | None,
) -> tuple[str | None, tuple[str, ...]]:
    """Build a stable title and tags from the hierarchy below a confirmed actor root."""

    if actor_directory is None or not actor:
        return None, ()
    actor_root = actor_directory.resolve(strict=False)
    source = path.resolve(strict=False)
    try:
        relative_to_actor = source.relative_to(actor_root)
        relative_to_library = source.relative_to(root.resolve(strict=False))
    except ValueError:
        return None, ()

    descendants = [
        value
        for raw in relative_to_actor.parts[:-1]
        if (value := _clean_hierarchy_segment(raw))
    ]
    if not descendants:
        return None, ()
    raw_stem = _remove_site_prefix(path.stem)
    suffix = _generic_suffix(raw_stem)
    if suffix and _VIDEO_PART.fullmatch(unicodedata.normalize("NFKC", raw_stem).strip()):
        suffix = f"v{suffix}"
    media_name = suffix or _clean_hierarchy_segment(raw_stem, preserve_identifier=True)
    title_parts = [actor.strip(), *descendants, media_name]
    compact_parts = [value for value in title_parts if value]
    title = "-".join(dict.fromkeys(compact_parts))

    actor_key = unicodedata.normalize("NFKC", actor).casefold().strip()
    tags = []
    for raw in relative_to_library.parts[:-1]:
        value = _clean_hierarchy_segment(raw)
        if not value or unicodedata.normalize("NFKC", value).casefold().strip() == actor_key:
            continue
        if value.casefold() in {"china", "国产", "國產"}:
            continue
        tags.append(value)
    return title or None, tuple(dict.fromkeys(tags))


def _clean_hierarchy_segment(value: str, *, preserve_identifier: bool = False) -> str:
    normalized = unicodedata.normalize("NFKC", value).strip(" .-_—")
    normalized = _remove_site_prefix(normalized)
    if not preserve_identifier:
        normalized = _clean_local_name(normalized)
    normalized = re.sub(r"\s*-\s*", "-", normalized)
    normalized = re.sub(r"(?<=[\u3400-\u9fff])\s+(?=[A-Za-z0-9\u3400-\u9fff])", "", normalized)
    normalized = re.sub(r"(?<=[A-Za-z0-9])\s+(?=[\u3400-\u9fff])", "", normalized)
    return " ".join(normalized.split()).strip(" .-_—")


def _local_title(raw_stem: str, hints: IdentityHints, subject: str | None) -> str:
    normalized_stem = _remove_site_prefix(raw_stem)
    suffix = _generic_suffix(normalized_stem)
    if suffix is not None:
        confirmed_actor = (
            next(iter(hints.actors), None) if "directory-actor:confirmed" in hints.alias_evidence else None
        )
        base = (
            confirmed_actor
            or subject
            or next(iter(hints.actors), None)
            or hints.title
            or hints.code
            or "未命名影片"
        )
        return f"{base}_{suffix}" if suffix else base

    cleaned = _clean_local_name(normalized_stem)
    if _OPAQUE_FILE.match(normalized_stem) and subject:
        opaque = re.sub(r"\s+", "_", normalized_stem)
        return f"{subject}_{opaque}"
    if cleaned:
        return cleaned
    return subject or hints.title or hints.code or "未命名影片"


def _nearest_subject(path: Path, root: Path, code: str | None) -> str | None:
    relative = _relative_path(path, root)
    directory_names = tuple(reversed(relative.parts[:-1]))
    for name in directory_names:
        subject = _subject_from_directory(name, code)
        if subject:
            return subject
    return None


def _subject_from_directory(name: str, code: str | None) -> str | None:
    normalized = unicodedata.normalize("NFKC", name).strip()
    if not normalized or normalized.casefold() in _TAXONOMY_NAMES:
        return None
    if code and normalize_identity_value(code) in normalize_identity_value(normalized):
        return None
    if handle := _HANDLE.search(normalized):
        return handle.group(1)
    for match in _BRACKETED.finditer(normalized):
        candidate = match.group(1).strip().lstrip("@")
        if _is_subject(candidate):
            return candidate
    if hashtag := _HASHTAG.search(normalized):
        return hashtag.group(1)
    cleaned = _clean_local_name(normalized)
    if _is_subject(cleaned):
        return cleaned
    return None


def _is_subject(value: str) -> bool:
    compact = value.strip()
    if not compact or len(compact) > 32 or compact.casefold() in _TAXONOMY_NAMES:
        return False
    if re.fullmatch(r"(?i)\d+[vp](?:\+\d+[gp])?", compact):
        return False
    marketing = ("合集", "最新", "付费", "福利", "视频", "作品", "泄密", "推荐")
    return not any(word in compact for word in marketing)


def _generic_suffix(raw_stem: str) -> str | None:
    normalized = unicodedata.normalize("NFKC", raw_stem).strip()
    if match := _VIDEO_RANGE_PART.fullmatch(normalized):
        return f"v{match.group(1)}-{match.group(2)}"
    if match := _VIDEO_PART.fullmatch(normalized):
        return match.group(1) or match.group(2) or ""
    if match := _PREFIXED_PART.fullmatch(normalized):
        number = match.group(2) or match.group(3) or ""
        return f"{match.group(1).upper()}{number}"
    if match := _NUMBERED_COPY.fullmatch(normalized):
        return f"{match.group(1)}_{match.group(2)}"
    if _PLAIN_NUMBER.fullmatch(normalized):
        return normalized
    if match := _GENERIC_WORD_PART.fullmatch(normalized):
        return match.group(1) or ""
    return None


def _is_generic_file_name(raw_stem: str) -> bool:
    normalized_stem = _remove_site_prefix(raw_stem)
    return _generic_suffix(normalized_stem) is not None or _OPAQUE_FILE.match(normalized_stem) is not None


def _clean_local_name(value: str) -> str:
    return re.sub(r"^[-—_✨❤⚫\uFE0F ]+", "", clean_stem(_remove_site_prefix(value))).strip()


def _remove_site_prefix(value: str) -> str:
    return _SITE_PREFIX.sub("", unicodedata.normalize("NFKC", value).strip())


def _relative_path(path: Path, root: Path) -> Path:
    try:
        return path.relative_to(root)
    except ValueError:
        return path


def _language_for_category(category: MediaCategory) -> str | None:
    return {
        MediaCategory.JAPAN: "ja",
        MediaCategory.CHINA: "zh",
        MediaCategory.KOREA: "ko",
        MediaCategory.EUROPE: "en",
        MediaCategory.OTHER: None,
    }[category]
