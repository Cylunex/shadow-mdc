"""Independent catalog classification dimensions and initial vocabulary."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .tag_registry import fold_tag

DimensionKey = Literal["contents", "types", "genres", "forms", "languages", "subtitle_languages", "regions"]


@dataclass(frozen=True)
class ClassificationDimension:
    id: DimensionKey
    name: str
    description: str


DIMENSIONS: tuple[ClassificationDimension, ...] = (
    ClassificationDimension(
        "contents", "内容体系", "按 JAV、国产原创、欧美等内容体系归类。与制作地区及音轨语言分别保存。"
    ),
    ClassificationDimension("types", "作品类型", "电影、剧集、动画、纪录片等作品大类。"),
    ClassificationDimension("genres", "题材", "剧情、喜剧、悬疑等内容题材。支持多选。"),
    ClassificationDimension("forms", "表现形式", "长片、短片、合集、自拍、VR 等呈现形式。"),
    ClassificationDimension(
        "languages", "音轨语言", "作品中实际使用的语言。不会根据内容体系或制作地区推断。"
    ),
    ClassificationDimension("subtitle_languages", "字幕语言", "字幕提供的语言、与音轨语言独立。"),
    ClassificationDimension("regions", "制作地区", "作品明确的制作国家或地区。与音轨语言分别保存。"),
)
DIMENSION_KEYS = tuple(dimension.id for dimension in DIMENSIONS)


class ClassificationSelection(BaseModel):
    model_config = ConfigDict(extra="forbid")
    contents: list[str] = Field(default_factory=list, max_length=100)
    types: list[str] = Field(default_factory=list, max_length=100)
    genres: list[str] = Field(default_factory=list, max_length=100)
    forms: list[str] = Field(default_factory=list, max_length=100)
    languages: list[str] = Field(default_factory=list, max_length=100)
    subtitle_languages: list[str] = Field(default_factory=list, max_length=100)
    regions: list[str] = Field(default_factory=list, max_length=100)

    def selections(self) -> dict[DimensionKey, list[str]]:
        """Typed dimension values without reflection or dynamic model attributes."""
        return {
            "contents": self.contents,
            "types": self.types,
            "genres": self.genres,
            "forms": self.forms,
            "languages": self.languages,
            "subtitle_languages": self.subtitle_languages,
            "regions": self.regions,
        }


@dataclass(frozen=True)
class ClassificationDefinition:
    id: str
    dimension: DimensionKey
    name: str
    aliases: tuple[str, ...] = ()


def _terms(
    dimension: DimensionKey,
    rows: tuple[tuple[str, str, tuple[str, ...]], ...],
) -> tuple[ClassificationDefinition, ...]:
    return tuple(
        ClassificationDefinition(f"{dimension}:{key}", dimension, name, aliases)
        for key, name, aliases in rows
    )


_LANGUAGE_ROWS: tuple[tuple[str, str, tuple[str, ...]], ...] = (
    ("zh", "中文", ("zh", "zho", "chi", "Chinese", "汉语", "普通话", "Mandarin")),
    ("yue", "粤语", ("yue", "Cantonese", "广东话")),
    ("ja", "日语", ("ja", "jpn", "Japanese", "日本語")),
    ("en", "英语", ("en", "eng", "English")),
    ("ko", "韩语", ("ko", "kor", "Korean", "한국어")),
    ("fr", "法语", ("fr", "fra", "fre", "French")),
    ("de", "德语", ("de", "deu", "ger", "German")),
    ("es", "西班牙语", ("es", "spa", "Spanish")),
    ("it", "意大利语", ("it", "ita", "Italian")),
    ("pt", "葡萄牙语", ("pt", "por", "Portuguese")),
    ("ru", "俄语", ("ru", "rus", "Russian")),
    ("th", "泰语", ("th", "tha", "Thai")),
    ("vi", "越南语", ("vi", "vie", "Vietnamese")),
    ("hi", "印地语", ("hi", "hin", "Hindi")),
    ("none", "无对白", ("zxx", "silent")),
)

DEFINITIONS: tuple[ClassificationDefinition, ...] = (
    *_terms(
        "contents",
        (
            ("jav", "JAV", ("jav",)),
            ("chinese", "国产原创", ("chinese",)),
            ("western", "欧美内容", ("western",)),
            ("korean", "韩系内容", ("korean",)),
            ("animation", "动画内容", ("animation",)),
        ),
    ),
    *_terms(
        "types",
        (
            ("movie", "电影", ("movie", "film")),
            ("series", "剧集", ("series", "电视剧")),
            ("animation", "动画", ("animation", "动漫")),
            ("documentary", "纪录片", ("documentary",)),
            ("variety", "综艺", ("variety",)),
            ("music-video", "音乐视频", ("MV", "music video")),
            ("live", "直播录像", ("live",)),
        ),
    ),
    *_terms(
        "genres",
        (
            ("drama", "剧情", ("drama", "戏剧")),
            ("comedy", "喜剧", ("comedy",)),
            ("romance", "爱情", ("romance",)),
            ("action", "动作", ("action",)),
            ("thriller", "惊悚", ("thriller",)),
            ("mystery", "悬疑", ("mystery",)),
            ("horror", "恐怖", ("horror",)),
            ("documentary", "纪录", ("documentary", "纪录片")),
            ("fantasy", "奇幻", ("fantasy",)),
            ("sci-fi", "科幻", ("sci-fi", "science fiction")),
            ("historical", "历史", ("history",)),
            ("reality", "纪实", ("reality",)),
        ),
    ),
    *_terms(
        "forms",
        (
            ("feature", "长片", ("feature",)),
            ("short", "短片", ("short",)),
            ("compilation", "合集", ("compilation", "总集篇")),
            ("episode", "单集", ("episode",)),
            ("selfie", "自拍", ("selfie",)),
            ("vr", "VR", ("vr", "虚拟现实")),
            ("interview", "访谈", ("interview",)),
            ("behind-scenes", "花絮", ("behind the scenes",)),
        ),
    ),
    *_terms("languages", _LANGUAGE_ROWS),
    *_terms(
        "subtitle_languages",
        (
            *_LANGUAGE_ROWS[:-1],
            ("none", "无字幕", ("none", "无字")),
        ),
    ),
    *_terms(
        "regions",
        (
            ("jp", "日本", ("JP", "JPN", "Japan", "日本国")),
            ("cn", "中国大陆", ("CN", "CHN", "China", "中国", "大陆")),
            ("hk", "中国香港", ("HK", "Hong Kong", "香港")),
            ("tw", "中国台湾", ("TW", "Taiwan", "台湾")),
            ("kr", "韩国", ("KR", "KOR", "Korea", "South Korea")),
            ("us", "美国", ("US", "USA", "United States")),
            ("gb", "英国", ("GB", "GBR", "UK", "United Kingdom")),
            ("fr", "法国", ("FR", "France")),
            ("de", "德国", ("DE", "Germany")),
            ("es", "西班牙", ("ES", "Spain")),
            ("it", "意大利", ("IT", "Italy")),
            ("ca", "加拿大", ("CA", "Canada")),
            ("au", "澳大利亚", ("AU", "Australia")),
            ("ru", "俄罗斯", ("RU", "Russia")),
            ("th", "泰国", ("TH", "Thailand")),
            ("in", "印度", ("IN", "India")),
        ),
    ),
    *(
        ClassificationDefinition(f"{dimension.id}:unknown", dimension.id, "待补", ("unknown",))
        for dimension in DIMENSIONS
    ),
)
DEFINITION_ALIASES: dict[tuple[str, str], ClassificationDefinition] = {
    (term.dimension, fold_tag(value)): term
    for term in DEFINITIONS
    for value in (term.id, term.name, *term.aliases)
}
FAMILY_CONTENTS = {key: f"contents:{key}" for key in ("jav", "chinese", "western", "korean", "animation")}
