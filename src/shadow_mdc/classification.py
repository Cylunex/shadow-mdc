import re
import unicodedata
from dataclasses import dataclass

from .enums import ContentFamily, MediaCategory

_HANGUL = re.compile(r"[\uac00-\ud7a3]")
_KANA = re.compile(r"[\u3040-\u30ff\u31f0-\u31ff]")
_HAN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]")
_LATIN_WORD = re.compile(r"(?i)(?<![a-z])[a-z]{2,}(?![a-z])")

_CHINA_MARKERS = (
    "china",
    "chinese",
    "cn porn",
    "91porn",
    "91制片",
    "国产",
    "國產",
    "华语",
    "華語",
    "大陆",
    "大陸",
    "台湾",
    "台灣",
    "中文",
    "麻豆",
    "探花",
    "糖心",
    "蜜桃",
    "果冻",
    "果凍",
    "天美",
    "星空",
    "皇家华人",
    "皇家華人",
    "精东",
    "精東",
    "自拍",
    "主播",
    "网红",
    "網紅",
    "偷拍",
)
_JAPAN_MARKERS = ("japan", "japanese", "jav", "日本", "有码", "有碼", "无码", "無碼")
_KOREA_MARKERS = ("korea", "korean", "한국", "韩国", "韓國")
_EUROPE_MARKERS = ("europe", "western", "欧美", "歐美", "english", "英語", "英语")
_ANIMATION_MARKERS = ("anime", "animation", "hentai", "cartoon", "动漫", "動漫", "动画", "動畫")
_OTHER_MARKERS = (
    "other",
    "unknown",
    "uncategorized",
    "未知",
    "未分类",
    "未分類",
    "其他",
    "其它",
)
_CONTROL_NAMES = frozenset(
    {
        "other",
        "unknown",
        "uncategorized",
        "test",
        "测试",
        "未知",
        "未分类",
        "未分類",
        "其他",
        "其它",
        *_JAPAN_MARKERS,
    }
)


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

    normalized_values = tuple(
        unicodedata.normalize("NFKC", value).casefold().strip() for value in values if value
    )
    text = " ".join(normalized_values)
    content_values: list[str] = []
    for value in normalized_values:
        if value in _CONTROL_NAMES:
            continue
        without_jav_taxonomy = value
        for marker in _JAPAN_MARKERS:
            without_jav_taxonomy = without_jav_taxonomy.replace(marker, " ")
        if cleaned := " ".join(without_jav_taxonomy.split()):
            content_values.append(cleaned)
    content_text = " ".join(content_values)
    marker_rules = (
        (_CHINA_MARKERS, ContentFamily.CHINESE, MediaCategory.CHINA, "marker:china"),
        (_KOREA_MARKERS, ContentFamily.KOREAN, MediaCategory.KOREA, "marker:korea"),
        (_EUROPE_MARKERS, ContentFamily.WESTERN, MediaCategory.EUROPE, "marker:europe"),
    )
    for markers, family, category, reason in marker_rules:
        if any(marker in text for marker in markers):
            return MediaClassification(family=family, category=category, reason=reason)

    if any(marker in text for marker in _ANIMATION_MARKERS):
        return MediaClassification(ContentFamily.ANIMATION, MediaCategory.OTHER, "marker:animation")

    if _HANGUL.search(content_text):
        return MediaClassification(ContentFamily.KOREAN, MediaCategory.KOREA, "script:hangul")
    if _KANA.search(content_text):
        return MediaClassification(ContentFamily.UNKNOWN, MediaCategory.OTHER, "script:kana:no-code")
    if _HAN.search(content_text):
        return MediaClassification(ContentFamily.CHINESE, MediaCategory.CHINA, "script:han")
    if len(_LATIN_WORD.findall(content_text)) >= 2:
        return MediaClassification(ContentFamily.WESTERN, MediaCategory.EUROPE, "script:latin")
    if any(marker in text for marker in _OTHER_MARKERS):
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
