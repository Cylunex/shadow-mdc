"""Reject non-JAV studio/label/fuzzy names when creating or importing actors.

Mirrors cleanup_non_jav_mis_tagged_actors patterns (麻豆/探花/传媒/制片厂…).
"""

from __future__ import annotations

import re
import unicodedata

EXPLICIT_STUDIO_LABELS: frozenset[str] = frozenset(
    {
        "91制片厂",
        "OnlyFans华语",
        "乐播传媒",
        "大象传媒",
        "天美传媒",
        "天美女郎",
        "果冻传媒",
        "果冻女孩",
        "精东影业",
        "糖心Vlog",
        "糖心女孩",
        "皇家华人",
        "星空无限传媒",
        "起点传媒",
        "麻豆",
        "麻豆传媒",
        "蜜桃影像",
        "杏吧",
        "海角社区",
        "推特华语博主",
        "推特福利姬",
        "反差博主",
        "丝足福利姬",
        "91大神",
        "91小严",
        "91小宝",
        "91瓜弟",
        "91探花",
        "夯先生",
        "唐伯虎",
        "弟弟竹竹",
    }
)

EXPLICIT_FUZZY_NAMES: frozenset[str] = frozenset(
    {
        "Amateur",
        "Unknown Guy",
        "Japanese Girl",
        "未知",
        "素人",
        "匿名",
        "不明",
        "无",
        "暂无",
        "N/A",
        "n/a",
        "NA",
        "null",
        "None",
        "演员",
        "女优",
        "女优名",
    }
)

KEEP_BORDERLINE: frozenset[str] = frozenset(
    {
        "蜜桃酱",
        "璇璇SWAG",
        "雪碧SWAG",
        "HongKongDoll",
        "完具",
        "国服第一瑶",
        "江南第一深情",
        "Naimi奶咪",
        "Nana Taipei",
        "Natasha Nice",
        "软萌白虎",
        "长腿白虎小优",
        "香草少女M",
        "粉色小猪",
        "粉色情人",
        "玉足女王",
        "一条肌肉狗",
        "kaCi脆脆",
    }
)

_PREFIX_SERIES = ("探花", "约炮", "探店", "杏吧")
_SUFFIX_SERIES = ("探花",)
_STUDIO_TOKENS = ("传媒", "制片厂", "影业", "影像", "Vlog", "vlog")
_FUZZY_EXACT = re.compile(
    r"^(?:未知|素人|匿名|不明|暂无|无|女优|演员|演员\d+|女优\d+|\d+)$",
    re.UNICODE,
)


def normalize_actor_name(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def classify_non_jav_actor_rejection(name: str) -> str | None:
    """Return rejection reason (`studio_label` / `fuzzy`) or None if allowed."""

    cleaned = name.strip()
    if not cleaned:
        return "fuzzy"
    if cleaned in KEEP_BORDERLINE:
        return None
    if cleaned in EXPLICIT_FUZZY_NAMES or _FUZZY_EXACT.fullmatch(cleaned):
        return "fuzzy"
    if cleaned in EXPLICIT_STUDIO_LABELS:
        return "studio_label"
    for prefix in _PREFIX_SERIES:
        if cleaned.startswith(prefix) and cleaned != prefix:
            return "studio_label"
    for suffix in _SUFFIX_SERIES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return "studio_label"
    for token in _STUDIO_TOKENS:
        if token in cleaned and cleaned not in KEEP_BORDERLINE:
            if cleaned.endswith(token) or cleaned.endswith("传媒") or cleaned.endswith("影业"):
                return "studio_label"
    return None


def reject_non_jav_studio_label(name: str) -> str | None:
    """Human-readable rejection detail, or None when allowed."""

    reason = classify_non_jav_actor_rejection(name)
    if reason is None:
        return None
    if reason == "studio_label":
        return f"拒绝将片商/工作室标签「{name.strip()}」登记为演员"
    return f"拒绝模糊/占位名称「{name.strip()}」登记为演员"
