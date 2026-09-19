"""Normalize provider genre tags to stable display labels (CN or loanword).

Only translates / aliases / filters existing provider tags — never invents genres
for works that lack them. Internal seed and source markers are blacklisted from
display and facets.
"""

from __future__ import annotations

import unicodedata
from collections import Counter
from collections.abc import Iterable, Sequence

# Costume / preference tags the filter bar should keep visible when count > 0.
COMMON_FILTER_TAGS: tuple[str, ...] = (
    "黑丝",
    "丝袜",
    "网袜",
    "高跟鞋",
    "内衣",
    "巨乳",
    "美腿",
    "美尻",
    "制服",
    "Cosplay",
    "眼镜",
    "口交",
    "深喉",
    "中出",
    "颜射",
    "射精",
    "潮吹",
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
    "贫乳",
    "娇小",
    "苗条",
    "多人",
    "女同",
    "素人",
    "亚洲",
    "日本",
    "POV",
    "NTR",
    "BBC",
    "BBW",
    "BDSM",
    "捆绑",
    "玩具",
    "自慰",
    "按摩",
    "露出",
    "户外",
    "里番",
    "动漫",
    "VR",
    "4K",
    "单体作品",
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
)

# Canonical CN label -> synonym forms (JP / EN / CN variants). Keys are display order.
_CANONICAL_SYNONYMS: dict[str, tuple[str, ...]] = {
    "黑丝": (
        "黑丝",
        "黑丝袜",
        "黒ストッキング",
        "黒スト",
        "黒タイツ",
        "黑色丝袜",
        "black stockings",
        "black pantyhose",
    ),
    "丝袜": (
        "丝袜",
        "絲襪",
        "パンスト",
        "パンスト・タイツ",
        "ストッキング",
        "タイツ",
        "pantyhose",
        "stockings",
        "tights",
    ),
    "网袜": (
        "网袜",
        "網襪",
        "網タイツ",
        "fishnet",
        "fishnets",
        "fishnet stockings",
    ),
    "高跟鞋": (
        "高跟鞋",
        "ハイヒール",
        "ピンヒール",
        "high heels",
        "high-heels",
        "heels",
    ),
    "内衣": (
        "内衣",
        "內衣",
        "ランジェリー",
        "lingerie",
    ),
    "巨乳": (
        "巨乳",
        "大奶",
        "爆乳",
        "超乳",
        "美乳",
        "おっぱい",
        "巨乳フェチ",
        "big tits",
        "big-tits",
        "big-boobs",
        "big boobs",
        "busty",
        "huge breasts",
    ),
    "贫乳": (
        "贫乳",
        "貧乳",
        "貧乳・微乳",
        "微乳",
        "petite breasts",
        "small tits",
    ),
    "美尻": (
        "美尻",
        "巨尻",
        "尻フェチ",
        "美臀",
        "big ass",
        "big-ass",
        "booty",
    ),
    "美腿": (
        "美腿",
        "脚フェチ",
        "美脚",
        "長身",
        "legs",
        "beautiful legs",
    ),
    "娇小": (
        "娇小",
        "嬌小",
        "petite",
    ),
    "苗条": (
        "苗条",
        "苗條",
        "スレンダー",
        "slender",
        "skinny",
    ),
    "微胖": (
        "微胖",
        "ぽっちゃり",
        "chubby",
    ),
    "多毛": (
        "多毛",
        "剛毛",
        "hairy",
        "bush",
    ),
    "刮毛": (
        "刮毛",
        "パイパン",
        "shaved",
        "shaved pussy",
    ),
    "制服": (
        "制服",
        "uniform",
    ),
    "Cosplay": (
        "Cosplay",
        "コスプレ",
        "cosplay",
        "コスプ",
    ),
    "眼镜": (
        "眼镜",
        "眼鏡",
        "メガネ",
        "glasses",
    ),
    "口交": (
        "口交",
        "フェラ",
        "フェラチオ",
        "blowjob",
        "oral",
    ),
    "深喉": (
        "深喉",
        "イラマチオ",
        "deepthroat",
        "deep throat",
        "deep-throat",
    ),
    "吞精": (
        "吞精",
        "ごっくん",
        "swallow",
        "cum swallow",
    ),
    "中出": (
        "中出",
        "中出し",
        "内射",
        "內射",
        "孕ませ",
        "creampie",
        "nakadashi",
    ),
    "颜射": (
        "颜射",
        "顏射",
        "顔射",
        "ぶっかけ",
        "facial",
        "bukkake",
    ),
    "射精": (
        "射精",
        "cumshot",
        "cum shot",
        "cum-shot",
    ),
    "潮吹": (
        "潮吹",
        "潮吹き",
        "squirting",
        "squirt",
    ),
    "痴女": (
        "痴女",
        "ビッチ",
        "淫乱・ハード系",
        "slut",
        "nympho",
    ),
    "人妻": (
        "人妻",
        "人妻・主婦",
        "既婚婦女",
        "人妇",
        "主婦",
        "married woman",
        "wife",
        "義母",
    ),
    "MILF": (
        "MILF",
        "milf",
    ),
    "熟女": (
        "熟女",
        "mature",
        "mature woman",
    ),
    "继母": (
        "继母",
        "繼母",
        "stepmom",
        "step-mom",
        "stepmother",
        "step mother",
    ),
    "OL": (
        "OL",
        "女上司",
        "秘书",
        "秘書",
        "office lady",
        "office",
        "secretary",
    ),
    "护士": (
        "护士",
        "護士",
        "看護婦・ナース",
        "ナース",
        "nurse",
    ),
    "学生": (
        "学生",
        "學生",
        "女子校生",
        "女子大生",
        "女学生",
        "schoolgirl",
        "student",
    ),
    "女仆": (
        "女仆",
        "女僕",
        "メイド",
        "maid",
    ),
    "老师": (
        "老师",
        "老師",
        "女教師",
        "女教师",
        "teacher",
        "female teacher",
    ),
    "多人": (
        "多人",
        "3P・4P",
        "3P",
        "4P",
        "乱交",
        "亂交",
        "ハーレム",
        "gangbang",
        "threesome",
        "orgy",
        "harem",
    ),
    "女同": (
        "女同",
        "レズビアン",
        "レズキス",
        "lesbian",
        "yuri",
        "girl-on-girl",
        "girl on girl",
    ),
    "情侣": (
        "情侣",
        "情侶",
        "カップル",
        "couple",
        "couples",
    ),
    "素人": (
        "素人",
        "amateur",
    ),
    "亚洲": (
        "亚洲",
        "亞洲",
        "アジア",
        "アジア系",
        "asian",
        "asia",
    ),
    "日本": (
        "日本",
        "日本人",
        "japanese",
        "japan",
    ),
    "VR": (
        "VR",
        "vr",
        "VR専用",
        "ハイクオリティVR",
        "8KVR",
    ),
    "4K": (
        "4K",
        "4k",
        "UHD",
    ),
    "单体作品": (
        "单体作品",
        "單體作品",
        "単体作品",
        "単体",
    ),
    "高清": (
        "高清",
        "ハイビジョン",
        "HD",
        "hd",
        "デジモ",
        "ギリモザ",
    ),
    "独占配信": (
        "独占配信",
        "獨占配信",
        "独占",
    ),
    "美少女": (
        "美少女",
        "かわいい",
        "可愛い",
        "美女",
        "美人",
        "cute",
        "beautiful girl",
    ),
    "接吻": (
        "接吻",
        "キス・接吻",
        "キス",
        "kiss",
    ),
    "高潮": (
        "高潮",
        "アクメ・オーガズム",
        "オーガズム",
        "orgasm",
    ),
    "后入": (
        "后入",
        "後入",
        "バック",
        "後背位",
        "doggystyle",
        "doggy style",
        "doggy-style",
    ),
    "骑乘": (
        "骑乘",
        "骑乘位",
        "騎乗位",
        "cowgirl",
    ),
    "反骑乘": (
        "反骑乘",
        "逆騎乗位",
        "reverse-cowgirl",
        "reverse cowgirl",
    ),
    "正常位": (
        "正常位",
        "missionary",
    ),
    "乳交": (
        "乳交",
        "パイズリ",
        "paizuri",
        "titjob",
    ),
    "NTR": (
        "NTR",
        "寝取り・寝取られ・NTR",
        "寝取り",
        "寝取られ",
        "netorare",
        "cuckold",
        "绿帽",
        "綠帽",
    ),
    "POV": (
        "POV",
        "pov",
        "主观",
        "主觀",
        "主観",
    ),
    "BBC": (
        "BBC",
        "bbc",
        "big black cock",
        "big-black-cock",
    ),
    "BBW": (
        "BBW",
        "bbw",
    ),
    "Interracial": (
        "Interracial",
        "interracial",
        "異人種",
    ),
    "Latina": (
        "Latina",
        "latina",
        "latino",
        "latin",
    ),
    "Ebony": (
        "Ebony",
        "ebony",
        "black girl",
    ),
    "Blonde": (
        "Blonde",
        "blonde",
        "blond",
        "金髪",
    ),
    "Brunette": (
        "Brunette",
        "brunette",
        "brown hair",
        "黒髪",
    ),
    "Redhead": (
        "Redhead",
        "redhead",
        "red hair",
        "赤髪",
    ),
    "Shemale": (
        "Shemale",
        "shemale",
        "transexual",
        "transsexual",
        "TS",
        "ts",
        "ニューハーフ",
    ),
    "姐姐": (
        "姐姐",
        "お姉さん",
        "oneesan",
    ),
    "出汗": (
        "出汗",
        "汗だく",
        "sweaty",
    ),
    "足交": (
        "足交",
        "足コキ",
        "footjob",
    ),
    "手交": (
        "手交",
        "手コキ",
        "handjob",
    ),
    "戏剧": (
        "戏剧",
        "戲劇",
        "ドラマ",
        "drama",
    ),
    "羞辱": (
        "羞辱",
        "辱め",
        "羞恥",
        "humiliation",
    ),
    "不倫": (
        "不倫",
        "不伦",
        "affair",
    ),
    "合集": (
        "合集",
        "ベスト・総集編",
        "女優ベスト・総集編",
        "4時間以上作品",
        "16時間以上作品",
        "best",
        "compilation",
    ),
    "出道作": (
        "出道作",
        "デビュー作品",
        "debut",
    ),
    "姐妹": (
        "姐妹",
        "姉・妹",
        "sister",
    ),
    "自拍": (
        "自拍",
        "ハメ撮り",
        "個人撮影",
        "gonzo",
    ),
    "淫语": (
        "淫语",
        "淫語",
        "dirty talk",
    ),
    "偶像": (
        "偶像",
        "アイドル・芸能人",
        "アイドル",
        "idol",
    ),
    "捆绑": (
        "捆绑",
        "捆綁",
        "拘束",
        "監禁",
        "bondage",
        "restrained",
    ),
    "癖好": (
        "癖好",
        "フェチ",
        "fetish",
    ),
    "肛交": (
        "肛交",
        "アナル",
        "anal",
    ),
    "BDSM": (
        "BDSM",
        "bdsm",
        "SM",
        "sm",
    ),
    "按摩": (
        "按摩",
        "マッサージ・リフレ",
        "massage",
    ),
    "近亲": (
        "近亲",
        "近親",
        "近親相姦",
        "incest",
    ),
    "空姐": (
        "空姐",
        "スチュワーデス",
        "stewardess",
        "flight attendant",
    ),
    "女医": (
        "女医",
        "女醫生",
        "女医生",
        "doctor",
    ),
    "女搜查官": (
        "女搜查官",
        "女捜査官",
        "investigator",
    ),
    "泡泡浴": (
        "泡泡浴",
        "ヘルス・ソープ",
        "soapland",
    ),
    "无文胸": (
        "无文胸",
        "ノーブラ",
        "no bra",
    ),
    "油腻": (
        "油腻",
        "ローション・オイル",
        "lotion",
        "oil",
    ),
    "自慰": (
        "自慰",
        "オナニー",
        "masturbation",
        "solo",
        "独自",
    ),
    "玩具": (
        "玩具",
        "おもちゃ",
        "toys",
        "dildo",
        "vibrator",
        "バイブ",
        "ディルド",
    ),
    "试镜": (
        "试镜",
        "試鏡",
        "オーディション",
        "casting",
    ),
    "酒店": (
        "酒店",
        "ホテル",
        "hotel",
    ),
    "海滩": (
        "海滩",
        "海灘",
        "ビーチ",
        "beach",
    ),
    "淋浴": (
        "淋浴",
        "シャワー",
        "shower",
    ),
    "浴室": (
        "浴室",
        "風呂",
        "bathroom",
        "bath",
    ),
    "厨房": (
        "厨房",
        "廚房",
        "キッチン",
        "kitchen",
    ),
    "车震": (
        "车震",
        "車震",
        "車内",
        "car",
        "car sex",
    ),
    "露出": (
        "露出",
        "公衆",
        "public",
        "公共",
    ),
    "户外": (
        "户外",
        "戶外",
        "野外",
        "outdoor",
        "outdoors",
    ),
    "摄像头": (
        "摄像头",
        "攝像頭",
        "webcam",
        "cam",
    ),
    "里番": (
        "里番",
        "裏番",
        "hentai",
    ),
    "动漫": (
        "动漫",
        "動漫",
        "アニメ",
        "anime",
        "cartoon",
    ),
    "孕妇": (
        "孕妇",
        "孕婦",
        "妊婦",
        "pregnant",
    ),
    "母乳": (
        "母乳",
        "授乳",
        "lactation",
        "breastfeeding",
    ),
    "纹身": (
        "纹身",
        "紋身",
        "タトゥー",
        "tattoo",
        "tattoos",
    ),
    "穿孔": (
        "穿孔",
        "ピアス",
        "piercing",
        "piercings",
    ),
    "童贞": (
        "童贞",
        "童貞",
        "virgin",
    ),
    "M女": (
        "M女",
        "masochist",
    ),
    "M男": (
        "M男",
        "男M",
    ),
    "ギャル": (
        "ギャル",
        "gal",
        "gyaru",
    ),
    "长篇": (
        "长篇",
        "4時間以上作品",
    ),
}


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
    "Interracial",
    "Latina",
    "Ebony",
    "Blonde",
    "Brunette",
    "Redhead",
    "Shemale",
)


def _fold(value: str) -> str:
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _build_alias_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for canonical, synonyms in _CANONICAL_SYNONYMS.items():
        index[_fold(canonical)] = canonical
        for synonym in synonyms:
            key = _fold(synonym)
            # First canonical wins for overlapping aliases (e.g. 4時間以上作品).
            index.setdefault(key, canonical)
    return index


_ALIAS_TO_CANONICAL: dict[str, str] = _build_alias_index()
_PRIORITY_RANK: dict[str, int] = {name: index for index, name in enumerate(_DISPLAY_PRIORITY)}


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


def canonicalize_tag(raw: str | None) -> str | None:
    """Map one raw tag to a canonical display label, or None if noise / empty.

    Unknown non-noise tags are returned unchanged (trimmed NFKC) so provider
    genres without a synonym still appear.
    """

    if raw is None:
        return None
    text = unicodedata.normalize("NFKC", str(raw)).strip()
    if not text or is_noise_tag(text):
        return None
    mapped = _ALIAS_TO_CANONICAL.get(_fold(text))
    return mapped if mapped is not None else text


def normalize_tags(raw: Iterable[str] | None) -> list[str]:
    """Translate / alias / filter tags → deterministic, deduped display list."""

    if not raw:
        return []
    seen: set[str] = set()
    values: list[str] = []
    for item in raw:
        canonical = canonicalize_tag(item)
        if canonical is None:
            continue
        key = _fold(canonical)
        if key in seen:
            continue
        seen.add(key)
        values.append(canonical)
    values.sort(key=lambda name: (_PRIORITY_RANK.get(name, 10_000), _fold(name)))
    return values


def work_matches_tags(work_tags: Sequence[str] | None, selected: Sequence[str]) -> bool:
    """AND filter: every selected tag must match the work's normalized set.

    Selected values may be JP/EN/CN synonyms — they are canonicalized first.
    """

    wanted = [canonicalize_tag(item) for item in selected]
    wanted_clean = [item for item in wanted if item]
    if not wanted_clean:
        return True
    available = set(normalize_tags(work_tags))
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
    for tags in works_tags:
        for name in normalize_tags(tags):
            counter[name] += 1
    ranked = sorted(counter.items(), key=lambda item: (-item[1], _fold(item[0])))
    selected: list[tuple[str, int]] = ranked[: max(0, limit)]
    present = {name for name, _ in selected}
    for name in always_include:
        count = counter.get(name, 0)
        if count > 0 and name not in present:
            selected.append((name, count))
            present.add(name)
    selected.sort(key=lambda item: (-item[1], _PRIORITY_RANK.get(item[0], 10_000), _fold(item[0])))
    return selected


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
    """Build UI chips: normalized genres first, then light metadata."""

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
    return values[:max_total]

