"""Normalize provider genre tags to stable display labels (CN or loanword).

Only translates / aliases / filters existing provider tags — never invents genres
for works that lack them. Internal seed and source markers are blacklisted from
display and facets.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence
from functools import lru_cache

from .tag_registry import TAG_REGISTRY, TagDefinition, TagReference, fold_tag

# Costume / preference tags the filter bar should keep visible when count > 0.
COMMON_FILTER_TAGS: tuple[str, ...] = (
    "黑丝",
    "丝袜",
    "网袜",
    "高跟鞋",
    "内衣",
    "无文胸",
    "巨乳",
    "美腿",
    "美尻",
    "制服",
    "Cosplay",
    "眼镜",
    "口交",
    "深喉",
    "乳交",
    "中出",
    "颜射",
    "射精",
    "吞精",
    "潮吹",
    "后入",
    "骑乘",
    "反骑乘",
    "正常位",
    "痴女",
    "人妻",
    "MILF",
    "熟女",
    "OL",
    "护士",
    "学生",
    "女仆",
    "老师",
    "继母",
    "姐姐",
    "姐妹",
    "贫乳",
    "娇小",
    "苗条",
    "微胖",
    "刮毛",
    "多毛",
    "多人",
    "女同",
    "情侣",
    "素人",
    "亚洲",
    "日本",
    "POV",
    "NTR",
    "不伦",
    "BBC",
    "BBW",
    "BDSM",
    "捆绑",
    "玩具",
    "自慰",
    "按摩",
    "露出",
    "户外",
    "酒店",
    "浴室",
    "淋浴",
    "海滩",
    "厨房",
    "车震",
    "摄像头",
    "自拍",
    "空姐",
    "女医",
    "偶像",
    "辣妹",
    "母乳",
    "穿孔",
    "油腻",
    "泡泡浴",
    "淫语",
    "羞辱",
    "戏剧",
    "里番",
    "动漫",
    "VR",
    "4K",
    "高清",
    "单体作品",
    "韩国",
    "角色扮演",
)

# Exact (casefolded) noise tags hidden from display + facets.
_EXACT_BLACKLIST: frozenset[str] = frozenset(
    {
        "jav",
        "chinese",
        "china",
        "korea",
        "korean",
        "western",
        "theporndb",
        "non-jav-seed",
        "jav-yearly-seed",
        "jav-vr-yearly-seed",
        "javranking",
        "javdb-top250",
        "javlibrary-top250",
        "most-awarded-videos",
        "hot-buzz",
        "magnet-saved",
        "manual-magnet",
        "blogger",
        "nv-pu-sa",
        "madou",
        "modelmediaasia",
        "onlyfans",
        "feature",
        "studio",
        "swag",
        "tanhua",
        "tianmei",
        "jelly",
        "xingkong",
        "91-tanhua",
        "fansdbhongkongdollonlyfans",
        "teamskeetxmodelmediaasia",
            "已认证素人",
        "已认证模特",
        "已认证情侣",
        "独家",
        "色情明星",
        "60帧",
        "60幀",
        "内嵌字幕",
        "內嵌字幕",
        "播客",
        "赌博",
        "賭博",
        "音乐",
        "音樂",
        "搞笑",
        "片场直击",
        "片場直擊",
        "视频激情",
        "視頻激情",
        "javdb-yearly-top250",
        "sample video",
        "サンプル動画",
        "サンプルビデオ",
        "forum",
        "non-jav",
        "fansly",
        "modelmediaus",
        "teamskeetxmodelmediaus",
        "ranking-enrich",
        "boss-rating",
        "strm-orphan",
        "shenzuo",
        "safety-review",
        "other",
        "gw great thanksgiving festival",
        "gw大感謝祭",
        "歳末新春セール",
        "オトナのサマーキャンペーン",
        "adult summer campaign",
        "new year's eve sale",
        "av大好きキャンペーン",
        "bigセール",
        "moodyzキャンペーン",
        "ファン感謝・訪問",
}
)

# Prefix patterns for seed / ranking / source noise (casefolded).
_PREFIX_BLACKLIST: tuple[str, ...] = (
    "jav-top-",
    "jav-vr-top-",
    "year-",
    "daily-",
    "weekly-",
    "swag",
    "non-jav-",
    "jav-yearly-",
    "jav-vr-yearly-",
    "javdb-yearly-",
    "javdb-top250",
    "manual-wishlist-",
    "actress-batch-",
    "yearly-",
)



# Preferred display order for known genres (then remaining alpha).
_DISPLAY_PRIORITY: tuple[str, ...] = (
    *COMMON_FILTER_TAGS,
    "高清",
    "独占配信",
    "美少女",
    "微胖",
    "多毛",
    "刮毛",
    "接吻",
    "高潮",
    "后入",
    "骑乘",
    "反骑乘",
    "正常位",
    "乳交",
    "吞精",
    "手交",
    "足交",
    "姐姐",
    "情侣",
    "戏剧",
    "合集",
    "出道作",
    "姐妹",
    "自拍",
    "偶像",
    "癖好",
    "肛交",
    "近亲",
    "空姐",
    "女医",
    "女搜查官",
    "试镜",
    "酒店",
    "海滩",
    "淋浴",
    "浴室",
    "厨房",
    "车震",
    "摄像头",
    "孕妇",
    "母乳",
    "纹身",
    "穿孔",
    "淫语",
    "羞辱",
    "辣妹",
    "泡泡浴",
    "油腻",
    "无文胸",
    "不伦",
    "童贞",
    "M女",
    "M男",
    "跨种族",
    "拉丁裔",
    "黑人",
    "金发",
    "棕发",
    "红发",
    "TS",
    "韩国",
    "角色扮演",
    "双龙",
)


def _fold(value: str) -> str:
    return fold_tag(value)


_PRIORITY_RANK: dict[str, int] = {
    tag.id: index for index, name in enumerate(_DISPLAY_PRIORITY)
    if (tag := TAG_REGISTRY.resolve(name)) is not None
}


def is_noise_tag(raw: str | None) -> bool:
    """Return True when a tag is an internal seed / source marker."""

    if raw is None:
        return True
    text = unicodedata.normalize("NFKC", raw).strip()
    if not text:
        return True
    key = text.casefold()
    if key in _EXACT_BLACKLIST:
        return True
    return any(key.startswith(prefix) for prefix in _PREFIX_BLACKLIST)


def resolve_tag(raw: str | None) -> TagDefinition | None:
    """Resolve a stable identity without changing stored provider values."""
    if raw is None or is_noise_tag(raw):
        return None
    tag = TAG_REGISTRY.resolve(raw)
    return tag if tag is not None and not is_noise_tag(tag.name) else None


def tag_id(raw: str | None) -> str | None:
    """Stable ID for a source value, historical name, alias or existing ID."""
    tag = resolve_tag(raw)
    return tag.id if tag else None


def synonyms_for(raw: str) -> tuple[str, ...]:
    """Known alias forms for any ID/name; excludes the current display name."""
    tag = resolve_tag(raw)
    return tuple(item for item in tag.aliases if item != tag.name) if tag else ()


def canonicalize_tag(raw: str | None) -> str | None:
    """Compatibility display label; use tag_id for identity comparisons."""
    tag = resolve_tag(raw)
    return tag.name if tag else None


def tag_refs(raw: Iterable[str] | None) -> list[TagReference]:
    """Deterministic, deduplicated identities and names, with noise excluded."""
    by_id: dict[str, TagDefinition] = {}
    for item in raw or ():
        tag = resolve_tag(item)
        if tag is not None:
            previous = by_id.get(tag.id)
            if previous is None or tag.name < previous.name:
                by_id[tag.id] = tag
    ordered = sorted(by_id.values(), key=lambda tag: (_PRIORITY_RANK.get(tag.id, 10_000), _fold(tag.name)))
    # Per-work responses carry references; aliases belong in the shared facet dictionary.
    return [TagReference(tag.id, tag.name) for tag in ordered]


def normalize_tags(raw: Iterable[str] | None) -> list[str]:
    """Translate / alias / filter tags → deterministic, deduped display list."""

    return [tag.name for tag in tag_refs(raw)]


def work_matches_tags(work_tags: Sequence[str] | None, selected: Sequence[str]) -> bool:
    """AND filter: every selected tag must match the work's normalized set.

    Selected values may be JP/EN/CN synonyms — they are canonicalized first.
    """

    wanted = [tag_id(item) for item in selected if not is_noise_tag(item)]
    if any(item is None for item in wanted):
        return False
    wanted_clean = [item for item in wanted if item]
    if not wanted_clean:
        return True
    available = {tag.id for tag in tag_refs(work_tags)}
    return all(tag in available for tag in wanted_clean)


def facet_tags(
    works_tags: Iterable[Sequence[str] | None],
    *,
    limit: int = 40,
    always_include: Sequence[str] = COMMON_FILTER_TAGS,
) -> list[tuple[str, int]]:
    """Popular normalized tags with counts for the filter bar.

    Excludes noise. Returns up to ``limit`` by count, then ensures any
    ``always_include`` tag with count > 0 is present.
    """

    counter: Counter[str] = Counter()
    names: dict[str, str] = {}
    for tags in works_tags:
        for tag in tag_refs(tags):
            counter[tag.id] += 1
            names.setdefault(tag.id, tag.name)
    ranked = sorted(counter.items(), key=lambda item: (-item[1], _fold(names[item[0]])))
    selected: list[tuple[str, int]] = ranked[: max(0, limit)]
    present = {name for name, _ in selected}
    for name in always_include:
        identity = tag_id(name)
        if identity is None:
            continue
        count = counter.get(identity, 0)
        if count > 0 and identity not in present:
            selected.append((identity, count))
            present.add(identity)
    selected.sort(key=lambda item: (
        -item[1], _PRIORITY_RANK.get(item[0], 10_000), _fold(names[item[0]]),
    ))
    return [(names[identity], count) for identity, count in selected]


_META_SKIP = frozenset({"unknown", "other", ""})


def display_chips(
    *,
    category: str | None = None,
    family: str | None = None,
    studio: str | None = None,
    label: str | None = None,
    series: str | None = None,
    tags: Sequence[str] | None = None,
    max_genres: int = 12,
    max_total: int = 16,
) -> list[str]:
    """Build UI chips: normalized genres first, then light metadata.

    Memoized on the chip inputs — a works-grid render rebuilds chips for every
    card, and studio/series/tag combinations repeat heavily.
    """

    return list(
        _display_chips_cached(
            category,
            family,
            studio,
            label,
            series,
            tuple(tags) if tags else (),
            max_genres,
            max_total,
        )
    )


@lru_cache(maxsize=8_192)
def _display_chips_cached(
    category: str | None,
    family: str | None,
    studio: str | None,
    label: str | None,
    series: str | None,
    tags: tuple[str, ...],
    max_genres: int,
    max_total: int,
) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()

    def add(raw: str | None) -> None:
        if not raw:
            return
        text = unicodedata.normalize("NFKC", str(raw)).strip()
        if not text or text.casefold() in _META_SKIP:
            return
        key = text.casefold()
        if key in seen:
            return
        seen.add(key)
        values.append(text)

    genres = normalize_tags(tags)[:max_genres]
    for genre in genres:
        add(genre)
    add(category)
    if family and family.casefold() not in {"unknown", "jav"}:
        add(family)
    add(studio)
    add(label)
    add(series)
    return tuple(values[:max_total])

