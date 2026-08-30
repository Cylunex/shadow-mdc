import re
import unicodedata
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

_WHITESPACE = re.compile(r"[\s\u200b\ufeff]+")
_ASCII_TOKEN = re.compile(r"[a-z0-9]+")
_ASCII_RULE = re.compile(r"[a-z0-9]+")


class FilterWords(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    words: tuple[str, ...] = Field(default_factory=tuple, max_length=5000)


@dataclass(frozen=True)
class FilterMatch:
    word: str
    relative_path: str


class MediaPathFilter:
    def __init__(self, words: Iterable[str] = ()):
        self._rules = tuple(
            (word, _normalize(word), _compact(word)) for word in _deduplicate(words) if _compact(word)
        )

    def match(self, path: Path, root: Path) -> FilterMatch | None:
        try:
            relative = path.relative_to(root)
        except ValueError:
            relative = path
        display_path = relative.as_posix()
        normalized_path = _normalize(display_path)
        compact_path = _compact(display_path)
        tokens = frozenset(_ASCII_TOKEN.findall(normalized_path))

        for original, normalized_rule, compact_rule in self._rules:
            if _is_ascii_token(normalized_rule):
                if normalized_rule in tokens or _matches_numbered_token(normalized_rule, tokens):
                    return FilterMatch(word=original, relative_path=display_path)
            elif compact_rule in compact_path:
                return FilterMatch(word=original, relative_path=display_path)
        return None


class FilterWordsStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> FilterWords:
        if not self._path.is_file():
            return FilterWords(words=default_filter_words())
        try:
            content = self._path.read_text(encoding="utf-8-sig")
        except (OSError, UnicodeError) as exc:
            raise ValueError(f"cannot read filter words: {exc}") from exc
        return FilterWords(words=_parse_filter_words(content))

    def save(self, rules: FilterWords) -> FilterWords:
        cleaned = FilterWords(words=_deduplicate(rules.words))
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text("\n".join(cleaned.words) + "\n", encoding="utf-8")
        temporary.replace(self._path)
        return cleaned


def default_filter_words() -> tuple[str, ...]:
    return (
        "社 區 最 新 情 報",
        "台 妹 子 線 上 現 場 直 播 各 式 花 式 表 演",
        "激情隨時看 tuu98.com",
        "台灣UU線上美女聊天 tuu32.com",
        "同城少妇美女学生妹都可以在这里找到 tuu96.com",
        "N房间的精彩直播 只有你想不到的刺激 tuu93.com",
        "美女荷官自拍被干",
        "UU直播 20年信誉保证 真诚邀请你体验真实的裸聊suu33.com",
        "UU直播 20年信誉保证拥有海量美女等你来选 suu37.com",
        "激情隨時看 suu26.com",
        "台湾uu美少女直播 20年信誉保证服务全球 suu28.com",
        "台灣UU線上美女聊天 suu27.com",
        "台湾uu美少女直播 20年信誉保证服务全球",
        "激情隨時看",
        "激情随时看",
        "台灣UU線上美女聊天",
        "台湾UU在线美女聊天",
        "20年信誉保证",
        "20年信譽保證",
        "同城少妇美女学生妹",
        "同城少婦美女學生妹",
        "sample",
        "trailer",
        "preview",
        "teaser",
        "试看",
        "試看",
        "预告片",
        "預告片",
        "防走失",
        "最新地址",
        "最新域名",
        "永久网址",
        "永久網址",
        "扫码关注",
        "掃碼關注",
        "关注公众号",
        "關注公眾號",
        "推广广告",
        "推廣廣告",
        "片头广告",
        "片頭廣告",
        "广告勿信",
        "廣告勿信",
        "收藏本站",
        "請收藏本站",
        "更多精彩视频",
        "更多精彩視頻",
        "18+游戏大全",
        "18+遊戲大全",
        "新 片 首 發 每 天 更 新 同 步 日 韓",
        "直播大秀",
        "xuu62.com",
        "有趣的台湾妹妹直播",
        "有趣的臺灣妹妹直播",
        "有趣的小视频",
        "uur9 3.com",
        "妹妹在精彩表演 哥哥快来大饱眼福",
        "千部好片盡在",
        "千部好片尽在",
        "乐鱼体育",
        "樂魚體育",
        "虎牙成人版",
        "凤凰娱乐",
        "鳳凰娛樂",
        "威尼斯人_真人棋牌",
        "下载APP",
        "下載APP",
    )


def _parse_filter_words(content: str) -> tuple[str, ...]:
    return _deduplicate(
        line.strip() for line in content.splitlines() if line.strip() and not line.lstrip().startswith("#")
    )


def _deduplicate(words: Iterable[str]) -> tuple[str, ...]:
    result: list[str] = []
    seen: set[str] = set()
    for raw_word in words:
        word = raw_word.strip()
        key = _compact(word)
        if not key or key in seen:
            continue
        seen.add(key)
        result.append(word)
    return tuple(result)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold()


def _compact(value: str) -> str:
    return _WHITESPACE.sub("", _normalize(value))


def _is_ascii_token(value: str) -> bool:
    return _ASCII_RULE.fullmatch(value) is not None


def _matches_numbered_token(rule: str, tokens: frozenset[str]) -> bool:
    return any(token.startswith(rule) and token[len(rule) :].isdigit() for token in tokens)
