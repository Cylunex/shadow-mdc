import re
import unicodedata
from dataclasses import dataclass

from .enums import ContentFamily, MediaCategory
from .reference_models import catalog_rules

_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"(?i)(?<![a-z])[a-z]{2,}(?![a-z])")


@dataclass(frozen=True, slots=True)
class MediaClassification:
    family: ContentFamily
    category: MediaCategory
    reason: str


def category_for_family(family: ContentFamily) -> MediaCategory:
    return {
        ContentFamily.JAV: MediaCategory.JAPAN,
        ContentFamily.CHINESE: MediaCategory.CHINA,
        ContentFamily.KOREAN: MediaCategory.KOREA,
        ContentFamily.WESTERN: MediaCategory.EUROPE,
        ContentFamily.ANIMATION: MediaCategory.OTHER,
        ContentFamily.UNKNOWN: MediaCategory.OTHER,
    }[family]


def classify_media(
    *values: str,
    detected_family: ContentFamily = ContentFamily.UNKNOWN,
    fallback: MediaCategory = MediaCategory.OTHER,
) -> MediaClassification:
    """Classify one asset; a detected identity always outranks language/path hints."""

    if detected_family is not ContentFamily.UNKNOWN:
        return MediaClassification(
            family=detected_family,
            category=category_for_family(detected_family),
            reason=f"identity:{detected_family.value}",
        )

    rules = catalog_rules()
    normalized_values = tuple(
        unicodedata.normalize("NFKC", value).casefold().strip() for value in values if value
    )
    text = " ".join(normalized_values)
    content_values: list[str] = []
    for value in normalized_values:
        if value in rules.control_names:
            continue
        without_jav_taxonomy = value
        for marker in rules.japan_markers:
            without_jav_taxonomy = without_jav_taxonomy.replace(marker, " ")
        if cleaned := " ".join(without_jav_taxonomy.split()):
            content_values.append(cleaned)
    content_text = " ".join(content_values)
    marker_rules = (
        (rules.china_markers, ContentFamily.CHINESE, MediaCategory.CHINA, "marker:china"),
        (rules.korea_markers, ContentFamily.KOREAN, MediaCategory.KOREA, "marker:korea"),
        (rules.europe_markers, ContentFamily.WESTERN, MediaCategory.EUROPE, "marker:europe"),
    )
    for markers, family, category, reason in marker_rules:
        if any(marker in text for marker in markers):
            return MediaClassification(family=family, category=category, reason=reason)

    if any(marker in text for marker in rules.animation_markers):
        return MediaClassification(ContentFamily.ANIMATION, MediaCategory.OTHER, "marker:animation")

    if _HANGUL.search(content_text):
        return MediaClassification(ContentFamily.KOREAN, MediaCategory.KOREA, "script:hangul")
    if _KANA.search(content_text):
        return MediaClassification(ContentFamily.UNKNOWN, MediaCategory.OTHER, "script:kana:no-code")
    if _HAN.search(content_text):
        return MediaClassification(ContentFamily.CHINESE, MediaCategory.CHINA, "script:han")
    if len(_LATIN_WORD.findall(content_text)) >= 2:
        return MediaClassification(ContentFamily.WESTERN, MediaCategory.EUROPE, "script:latin")
    if any(marker in text for marker in rules.other_markers):
        return MediaClassification(ContentFamily.UNKNOWN, MediaCategory.OTHER, "marker:other")
    if fallback is MediaCategory.JAPAN:
        return MediaClassification(
            ContentFamily.UNKNOWN,
            MediaCategory.OTHER,
            "fallback:japan:no-code",
        )
    fallback_family = {
        MediaCategory.JAPAN: ContentFamily.JAV,
        MediaCategory.CHINA: ContentFamily.CHINESE,
        MediaCategory.KOREA: ContentFamily.KOREAN,
        MediaCategory.EUROPE: ContentFamily.WESTERN,
        MediaCategory.OTHER: ContentFamily.UNKNOWN,
    }[fallback]
    return MediaClassification(fallback_family, fallback, f"fallback:{fallback.value}")
