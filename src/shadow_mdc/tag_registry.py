"""Tag identities and names. IDs are permanent; names and aliases may evolve."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from functools import lru_cache
from urllib.parse import quote, unquote


def fold_tag(value: str) -> str:
    """Comparison form shared by registry aliases and unknown tag identities."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


@dataclass(frozen=True)
class TagReference:
    id: str
    name: str


@dataclass(frozen=True)
class TagDefinition(TagReference):
    aliases: tuple[str, ...] = ()


class TagRegistry:
    """Immutable dictionary with explicit identities and reversible custom IDs."""

    def __init__(self, definitions: tuple[TagDefinition, ...]) -> None:
        self.definitions = definitions
        self._by_id: dict[str, TagDefinition] = {}
        self._by_alias: dict[str, TagDefinition] = {}
        for tag in definitions:
            if not re.fullmatch(r"tag:[a-z0-9]+(?:-[a-z0-9]+)*", tag.id):
                raise ValueError(f"Invalid tag ID: {tag.id}")
            if tag.id in self._by_id or not tag.name.strip():
                raise ValueError(f"Duplicate ID or empty name: {tag.id}")
            self._by_id[tag.id] = tag
            for alias in (tag.name, *tag.aliases):
                key = fold_tag(alias)
                if not key or key.startswith(("tag:", "raw:")):
                    raise ValueError(f"Invalid alias for {tag.id}")
                previous = self._by_alias.get(key)
                if previous is not None and previous.id != tag.id:
                    raise ValueError(f"Ambiguous tag alias: {alias}")
                self._by_alias[key] = tag
        self._cached_resolve = lru_cache(maxsize=16_384)(self._resolve)

    def resolve(self, raw: str) -> TagDefinition | None:
        """Resolve an ID, name or alias; keep unknown tags in a separate namespace."""
        return self._cached_resolve(raw)

    def _resolve(self, raw: str) -> TagDefinition | None:
        text = unicodedata.normalize("NFKC", raw).strip()
        key = fold_tag(text)
        if key in self._by_id:
            return self._by_id[key]
        if key in self._by_alias:
            return self._by_alias[key]
        if key.startswith("tag:") or not key:
            return None
        if text.startswith("raw:"):
            try:
                decoded = unquote(text[4:], errors="strict")
            except UnicodeError:
                return None
            if not decoded or text != "raw:" + quote(fold_tag(decoded), safe=""):
                return None
            # A custom ID stays custom even if a new built-in alias is added later.
            return TagDefinition(id=text, name=decoded)
        return TagDefinition(id="raw:" + quote(key, safe=""), name=text, aliases=(text,))


# Keep historical names in aliases when changing a name. Never derive IDs from names.
TAG_DEFINITIONS: tuple[TagDefinition, ...] = (
    TagDefinition(
        id="tag:black-stockings",
        name="黑丝",
        aliases=(
            "黑丝",
            "黑丝袜",
            "黒ストッキング",
            "黒スト",
            "黒タイツ",
            "黑色丝袜",
            "black stockings",
            "black pantyhose",
        ),
    ),
    TagDefinition(
        id="tag:stockings",
        name="丝袜",
        aliases=(
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
    ),
    TagDefinition(
        id="tag:fishnet",
        name="网袜",
        aliases=(
            "网袜",
            "網襪",
            "網タイツ",
            "fishnet",
            "fishnets",
            "fishnet stockings",
        ),
    ),
    TagDefinition(
        id="tag:high-heels",
        name="高跟鞋",
        aliases=(
            "高跟鞋",
            "ハイヒール",
            "ピンヒール",
            "high heels",
            "high-heels",
            "heels",
        ),
    ),
    TagDefinition(
        id="tag:lingerie",
        name="内衣",
        aliases=(
            "内衣",
            "內衣",
            "ランジェリー",
            "lingerie",
        ),
    ),
    TagDefinition(
        id="tag:big-breasts",
        name="巨乳",
        aliases=(
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
            "Beautiful Tits",
            "beautiful tits",
            "Huge Tits",
            "huge tits",
            "Big Tits Lover",
            "big tits lover",
        ),
    ),
    TagDefinition(
        id="tag:small-breasts",
        name="贫乳",
        aliases=(
            "贫乳",
            "貧乳",
            "貧乳・微乳",
            "微乳",
            "petite breasts",
            "small tits",
        ),
    ),
    TagDefinition(
        id="tag:buttocks",
        name="美尻",
        aliases=(
            "美尻",
            "巨尻",
            "尻フェチ",
            "美臀",
            "big ass",
            "big-ass",
            "booty",
            "肥臀",
            "ass",
            "Big Asses",
            "big asses",
            "Ass Lover",
            "ass lover",
            "Ass Fetish",
            "ass fetish",
        ),
    ),
    TagDefinition(
        id="tag:legs",
        name="美腿",
        aliases=(
            "美腿",
            "脚フェチ",
            "美脚",
            "長身",
            "legs",
            "beautiful legs",
            "Leg Fetish",
            "leg fetish",
        ),
    ),
    TagDefinition(
        id="tag:petite",
        name="娇小",
        aliases=(
            "娇小",
            "嬌小",
            "petite",
            "ミニ系",
            "Short Woman",
            "short woman",
        ),
    ),
    TagDefinition(
        id="tag:slim",
        name="苗条",
        aliases=(
            "苗条",
            "苗條",
            "スレンダー",
            "slender",
            "skinny",
        ),
    ),
    TagDefinition(
        id="tag:chubby",
        name="微胖",
        aliases=(
            "微胖",
            "ぽっちゃり",
            "chubby",
        ),
    ),
    TagDefinition(
        id="tag:hairy",
        name="多毛",
        aliases=(
            "多毛",
            "剛毛",
            "hairy",
            "bush",
            "Hairy Pussy",
            "hairy pussy",
        ),
    ),
    TagDefinition(
        id="tag:shaved",
        name="刮毛",
        aliases=(
            "刮毛",
            "パイパン",
            "shaved",
            "shaved pussy",
            "shaving",
            "Hairless Pussy",
            "hairless pussy",
        ),
    ),
    TagDefinition(
        id="tag:uniform",
        name="制服",
        aliases=(
            "制服",
            "uniform",
            "school uniform",
            "Academy Uniform",
            "academy uniform",
            "学生服",
            "セーラー服",
            "sailor uniform",
        ),
    ),
    TagDefinition(
        id="tag:cosplay",
        name="Cosplay",
        aliases=(
            "Cosplay",
            "コスプレ",
            "cosplay",
            "コスプ",
        ),
    ),
    TagDefinition(
        id="tag:glasses",
        name="眼镜",
        aliases=(
            "眼镜",
            "眼鏡",
            "メガネ",
            "めがね",
            "glasses",
        ),
    ),
    TagDefinition(
        id="tag:oral",
        name="口交",
        aliases=(
            "口交",
            "フェラ",
            "フェラチオ",
            "blowjob",
            "oral",
        ),
    ),
    TagDefinition(
        id="tag:deepthroat",
        name="深喉",
        aliases=(
            "深喉",
            "イラマチオ",
            "deepthroat",
            "deep throat",
            "deep-throat",
            "Face Fuck",
            "face fuck",
        ),
    ),
    TagDefinition(
        id="tag:swallowing",
        name="吞精",
        aliases=(
            "吞精",
            "ごっくん",
            "swallow",
            "cum swallow",
            "cum swallowing",
            "gokkun",
            "Cum In Mouth",
            "cum in mouth",
        ),
    ),
    TagDefinition(
        id="tag:creampie",
        name="中出",
        aliases=(
            "中出",
            "中出し",
            "内射",
            "內射",
            "孕ませ",
            "creampie",
            "nakadashi",
            "内射中出",
        ),
    ),
    TagDefinition(
        id="tag:facial",
        name="颜射",
        aliases=(
            "颜射",
            "顏射",
            "顔射",
            "ぶっかけ",
            "facial",
            "bukkake",
            "集体颜射",
            "集體顏射",
        ),
    ),
    TagDefinition(
        id="tag:ejaculation",
        name="射精",
        aliases=(
            "射精",
            "cumshot",
            "cum shot",
            "cum-shot",
        ),
    ),
    TagDefinition(
        id="tag:squirting",
        name="潮吹",
        aliases=(
            "潮吹",
            "潮吹き",
            "squirting",
            "squirt",
        ),
    ),
    TagDefinition(
        id="tag:femdom",
        name="痴女",
        aliases=(
            "痴女",
            "ビッチ",
            "淫乱・ハード系",
            "slut",
            "nympho",
            "Nymphomaniac",
            "nymphomaniac",
        ),
    ),
    TagDefinition(
        id="tag:married",
        name="人妻",
        aliases=(
            "人妻",
            "人妻・主婦",
            "既婚婦女",
            "人妇",
            "主婦",
            "married woman",
            "wife",
            "義母",
            "housewife",
            "housewives",
        ),
    ),
    TagDefinition(
        id="tag:milf",
        name="MILF",
        aliases=(
            "MILF",
            "milf",
            "辣妈",
            "辣媽",
            "MILF (30+)",
            "milf (30+)",
        ),
    ),
    TagDefinition(
        id="tag:mature",
        name="熟女",
        aliases=(
            "熟女",
            "mature",
            "mature woman",
        ),
    ),
    TagDefinition(
        id="tag:stepmother",
        name="继母",
        aliases=(
            "继母",
            "繼母",
            "stepmom",
            "step-mom",
            "stepmother",
            "step mother",
        ),
    ),
    TagDefinition(
        id="tag:office-lady",
        name="OL",
        aliases=(
            "OL",
            "女上司",
            "秘书",
            "秘書",
            "office lady",
            "office",
            "secretary",
            "Female Boss",
            "female boss",
        ),
    ),
    TagDefinition(
        id="tag:nurse",
        name="护士",
        aliases=(
            "护士",
            "護士",
            "看護婦・ナース",
            "ナース",
            "nurse",
            "nurses",
        ),
    ),
    TagDefinition(
        id="tag:student",
        name="学生",
        aliases=(
            "学生",
            "學生",
            "女子校生",
            "女子大生",
            "女学生",
            "schoolgirl",
            "student",
            "students",
            "校园",
            "校園",
            "大学",
            "大學",
            "学園もの",
        ),
    ),
    TagDefinition(
        id="tag:maid",
        name="女仆",
        aliases=(
            "女仆",
            "女僕",
            "メイド",
            "maid",
            "火辣保姆",
            "保姆",
            "babysitter",
        ),
    ),
    TagDefinition(
        id="tag:teacher",
        name="老师",
        aliases=(
            "老师",
            "老師",
            "女教師",
            "女教师",
            "teacher",
            "female teacher",
        ),
    ),
    TagDefinition(
        id="tag:group",
        name="多人",
        aliases=(
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
            "乱交群欢",
            "亂交群歡",
            "轮交",
            "輪交",
            "Threesome / Foursome",
            "threesome / foursome",
            "G*******g",
            "g*******g",
        ),
    ),
    TagDefinition(
        id="tag:lesbian",
        name="女同",
        aliases=(
            "女同",
            "レズビアン",
            "レズキス",
            "lesbian",
            "yuri",
            "girl-on-girl",
            "girl on girl",
            "レズ",
        ),
    ),
    TagDefinition(
        id="tag:couple",
        name="情侣",
        aliases=(
            "情侣",
            "情侶",
            "カップル",
            "couple",
            "couples",
        ),
    ),
    TagDefinition(
        id="tag:amateur",
        name="素人",
        aliases=(
            "素人",
            "amateur",
        ),
    ),
    TagDefinition(
        id="tag:asian",
        name="亚洲",
        aliases=(
            "亚洲",
            "亞洲",
            "アジア",
            "アジア系",
            "asian",
            "asia",
            "亚洲人",
            "亞洲人",
        ),
    ),
    TagDefinition(
        id="tag:japanese",
        name="日本",
        aliases=(
            "日本",
            "日本人",
            "japanese",
            "japan",
        ),
    ),
    TagDefinition(
        id="tag:vr",
        name="VR",
        aliases=(
            "VR",
            "vr",
            "VR専用",
            "ハイクオリティVR",
            "8KVR",
            "虚拟现实",
            "虛擬現實",
            "VR Porn",
            "vr porn",
        ),
    ),
    TagDefinition(
        id="tag:4k",
        name="4K",
        aliases=(
            "4K",
            "4k",
            "UHD",
            "4k Ultra HD",
            "4K Ultra HD",
            "4k-porn",
        ),
    ),
    TagDefinition(
        id="tag:solo-performer",
        name="单体作品",
        aliases=(
            "单体作品",
            "單體作品",
            "単体作品",
            "単体",
        ),
    ),
    TagDefinition(
        id="tag:hd",
        name="高清",
        aliases=(
            "高清",
            "ハイビジョン",
            "HD",
            "hd",
            "デジモ",
            "ギリモザ",
            "Hi-Def",
            "hi-def",
            "高畫質",
            "高画质",
        ),
    ),
    TagDefinition(
        id="tag:exclusive",
        name="独占配信",
        aliases=(
            "独占配信",
            "獨占配信",
            "独占",
            "Exclusive Distribution",
            "exclusive distribution",
        ),
    ),
    TagDefinition(
        id="tag:young-adult",
        name="美少女",
        aliases=(
            "美少女",
            "かわいい",
            "可愛い",
            "美女",
            "美人",
            "cute",
            "beautiful girl",
            "风情少女",
            "風情少女",
        ),
    ),
    TagDefinition(
        id="tag:kissing",
        name="接吻",
        aliases=(
            "接吻",
            "キス・接吻",
            "キス",
            "kiss",
            "kissing",
            "Kiss Kiss",
            "kiss kiss",
        ),
    ),
    TagDefinition(
        id="tag:orgasm",
        name="高潮",
        aliases=(
            "高潮",
            "アクメ・オーガズム",
            "オーガズム",
            "orgasm",
            "女性高潮",
        ),
    ),
    TagDefinition(
        id="tag:doggy",
        name="后入",
        aliases=(
            "后入",
            "後入",
            "バック",
            "後背位",
            "doggystyle",
            "doggy style",
            "doggy-style",
        ),
    ),
    TagDefinition(
        id="tag:cowgirl",
        name="骑乘",
        aliases=(
            "骑乘",
            "骑乘位",
            "騎乗位",
            "cowgirl",
            "riding",
        ),
    ),
    TagDefinition(
        id="tag:reverse-cowgirl",
        name="反骑乘",
        aliases=(
            "反骑乘",
            "逆騎乗位",
            "reverse-cowgirl",
            "reverse cowgirl",
        ),
    ),
    TagDefinition(
        id="tag:missionary",
        name="正常位",
        aliases=(
            "正常位",
            "missionary",
        ),
    ),
    TagDefinition(
        id="tag:titjob",
        name="乳交",
        aliases=(
            "乳交",
            "パイズリ",
            "paizuri",
            "titjob",
            "titty fucking",
            "tittyfuck",
            "tit fuck",
            "Titty Fuck",
            "titty fuck",
            "cum on tits",
        ),
    ),
    TagDefinition(
        id="tag:ntr",
        name="NTR",
        aliases=(
            "NTR",
            "寝取り・寝取られ・NTR",
            "寝取り",
            "寝取られ",
            "netorare",
            "cuckold",
            "绿帽",
            "綠帽",
            "娇妻偷吃",
            "嬌妻偷吃",
        ),
    ),
    TagDefinition(
        id="tag:pov",
        name="POV",
        aliases=(
            "POV",
            "pov",
            "主观",
            "主觀",
            "主観",
            "第一视角",
            "第一視角",
        ),
    ),
    TagDefinition(
        id="tag:bbc",
        name="BBC",
        aliases=(
            "BBC",
            "bbc",
            "big black cock",
            "big-black-cock",
        ),
    ),
    TagDefinition(
        id="tag:bbw",
        name="BBW",
        aliases=(
            "BBW",
            "bbw",
            "大号美女",
            "大號美女",
        ),
    ),
    TagDefinition(
        id="tag:interracial",
        name="跨种族",
        aliases=(
            "跨种族",
            "跨種族",
            "Interracial",
            "interracial",
            "異人種",
        ),
    ),
    TagDefinition(
        id="tag:latina",
        name="拉丁裔",
        aliases=(
            "拉丁裔",
            "拉丁裔美女",
            "拉丁",
            "Latina",
            "latina",
            "latino",
            "latin",
        ),
    ),
    TagDefinition(
        id="tag:ebony",
        name="黑人",
        aliases=(
            "黑人",
            "黑人女",
            "Ebony",
            "ebony",
            "black girl",
        ),
    ),
    TagDefinition(
        id="tag:blonde",
        name="金发",
        aliases=(
            "金发",
            "金髮",
            "金发女",
            "金髮女",
            "Blonde",
            "blonde",
            "blond",
            "金髪",
            "Blonde Hair (Female)",
            "blonde hair (female)",
        ),
    ),
    TagDefinition(
        id="tag:brunette",
        name="棕发",
        aliases=(
            "棕发",
            "棕髮",
            "深发女",
            "深髮女",
            "Brunette",
            "brunette",
            "brown hair",
            "黒髪",
        ),
    ),
    TagDefinition(
        id="tag:redhead",
        name="红发",
        aliases=(
            "红发",
            "紅髮",
            "红毛",
            "紅毛",
            "Redhead",
            "redhead",
            "red hair",
            "赤髪",
        ),
    ),
    TagDefinition(
        id="tag:transgender",
        name="TS",
        aliases=(
            "TS",
            "ts",
            "Shemale",
            "shemale",
            "transexual",
            "transsexual",
            "Trans",
            "trans",
            "ニューハーフ",
            "跨性别",
            "跨性別",
        ),
    ),
    TagDefinition(
        id="tag:older-sister",
        name="姐姐",
        aliases=(
            "姐姐",
            "お姉さん",
            "oneesan",
            "Older Sister",
            "older sister",
        ),
    ),
    TagDefinition(
        id="tag:sweat",
        name="出汗",
        aliases=(
            "出汗",
            "汗だく",
            "sweaty",
            "Sweating",
            "sweating",
        ),
    ),
    TagDefinition(
        id="tag:footjob",
        name="足交",
        aliases=(
            "足交",
            "足コキ",
            "footjob",
            "恋足",
            "戀足",
            "Foot Fetish",
            "foot fetish",
        ),
    ),
    TagDefinition(
        id="tag:handjob",
        name="手交",
        aliases=(
            "手交",
            "手コキ",
            "handjob",
        ),
    ),
    TagDefinition(
        id="tag:drama",
        name="戏剧",
        aliases=(
            "戏剧",
            "戲劇",
            "ドラマ",
            "drama",
        ),
    ),
    TagDefinition(
        id="tag:humiliation",
        name="羞辱",
        aliases=(
            "羞辱",
            "辱め",
            "羞恥",
            "humiliation",
            "Embarrassment",
            "embarrassment",
            "Shame",
            "shame",
        ),
    ),
    TagDefinition(
        id="tag:affair",
        name="不伦",
        aliases=(
            "不伦",
            "不倫",
            "係倫",
            "出轨",
            "出軌",
            "affair",
            "affairs",
            "cheating",
            "Cheating Wife",
            "cheating wife",
            "Adultery",
            "adultery",
        ),
    ),
    TagDefinition(
        id="tag:compilation",
        name="合集",
        aliases=(
            "合集",
            "ベスト・総集編",
            "女優ベスト・総集編",
            "4時間以上作品",
            "16時間以上作品",
            "best",
            "compilation",
            "Over 4 Hours",
            "over 4 hours",
        ),
    ),
    TagDefinition(
        id="tag:debut",
        name="出道作",
        aliases=(
            "出道作",
            "デビュー作品",
            "debut",
        ),
    ),
    TagDefinition(
        id="tag:sisters",
        name="姐妹",
        aliases=(
            "姐妹",
            "姉・妹",
            "sister",
            "stepsister",
            "step-sister",
            "step sister",
        ),
    ),
    TagDefinition(
        id="tag:selfie",
        name="自拍",
        aliases=(
            "自拍",
            "ハメ撮り",
            "個人撮影",
            "gonzo",
            "真人实拍",
        ),
    ),
    TagDefinition(
        id="tag:dirty-talk",
        name="淫语",
        aliases=(
            "淫语",
            "淫語",
            "dirty talk",
        ),
    ),
    TagDefinition(
        id="tag:idol",
        name="偶像",
        aliases=(
            "偶像",
            "アイドル・芸能人",
            "アイドル",
            "idol",
            "Idol & Celebrity",
            "idol & celebrity",
        ),
    ),
    TagDefinition(
        id="tag:bondage",
        name="捆绑",
        aliases=(
            "捆绑",
            "捆綁",
            "拘束",
            "監禁",
            "bondage",
            "restrained",
            "縛り・緊縛",
            "緊縛",
            "Ropes & Ties",
            "ropes & ties",
        ),
    ),
    TagDefinition(
        id="tag:fetish",
        name="癖好",
        aliases=(
            "癖好",
            "フェチ",
            "fetish",
            "恋物癖",
            "戀物癖",
            "その他フェチ",
        ),
    ),
    TagDefinition(
        id="tag:anal",
        name="肛交",
        aliases=(
            "肛交",
            "アナル",
            "anal",
            "爆菊",
            "アナルセックス",
        ),
    ),
    TagDefinition(
        id="tag:bdsm",
        name="BDSM",
        aliases=(
            "BDSM",
            "bdsm",
            "SM",
            "sm",
        ),
    ),
    TagDefinition(
        id="tag:massage",
        name="按摩",
        aliases=(
            "按摩",
            "マッサージ・リフレ",
            "massage",
            "Massage Parlor",
            "massage parlor",
        ),
    ),
    TagDefinition(
        id="tag:incest",
        name="近亲",
        aliases=(
            "近亲",
            "近親",
            "近親相姦",
            "incest",
            "继家庭幻想",
            "繼家庭幻想",
            "Stepfamily",
            "stepfamily",
        ),
    ),
    TagDefinition(
        id="tag:flight-attendant",
        name="空姐",
        aliases=(
            "空姐",
            "スチュワーデス",
            "stewardess",
            "flight attendant",
        ),
    ),
    TagDefinition(
        id="tag:doctor",
        name="女医",
        aliases=(
            "女医",
            "女醫生",
            "女医生",
            "doctor",
        ),
    ),
    TagDefinition(
        id="tag:investigator",
        name="女搜查官",
        aliases=(
            "女搜查官",
            "女捜査官",
            "investigator",
        ),
    ),
    TagDefinition(
        id="tag:bubble-bath",
        name="泡泡浴",
        aliases=(
            "泡泡浴",
            "ヘルス・ソープ",
            "soapland",
            "soap",
        ),
    ),
    TagDefinition(
        id="tag:braless",
        name="无文胸",
        aliases=(
            "无文胸",
            "ノーブラ",
            "no bra",
        ),
    ),
    TagDefinition(
        id="tag:oiled",
        name="油腻",
        aliases=(
            "油腻",
            "油膩",
            "ローション・オイル",
            "lotion",
            "oil",
            "oiled",
            "oily",
        ),
    ),
    TagDefinition(
        id="tag:masturbation",
        name="自慰",
        aliases=(
            "自慰",
            "オナニー",
            "masturbation",
            "solo",
            "独自",
            "女性自慰",
            "female masturbation",
            "手淫",
            "男性自慰",
        ),
    ),
    TagDefinition(
        id="tag:toys",
        name="玩具",
        aliases=(
            "玩具",
            "おもちゃ",
            "toys",
            "dildo",
            "vibrator",
            "バイブ",
            "ディルド",
            "性玩具",
            "佩戴式阳具",
            "佩戴式陽具",
            "Sex Toys",
            "sex toys",
            "電マ",
        ),
    ),
    TagDefinition(
        id="tag:casting",
        name="试镜",
        aliases=(
            "试镜",
            "試鏡",
            "オーディション",
            "casting",
        ),
    ),
    TagDefinition(
        id="tag:hotel",
        name="酒店",
        aliases=(
            "酒店",
            "ホテル",
            "hotel",
        ),
    ),
    TagDefinition(
        id="tag:beach",
        name="海滩",
        aliases=(
            "海滩",
            "海灘",
            "ビーチ",
            "beach",
        ),
    ),
    TagDefinition(
        id="tag:shower",
        name="淋浴",
        aliases=(
            "淋浴",
            "シャワー",
            "shower",
        ),
    ),
    TagDefinition(
        id="tag:bathroom",
        name="浴室",
        aliases=(
            "浴室",
            "風呂",
            "bathroom",
            "bath",
            "お風呂",
        ),
    ),
    TagDefinition(
        id="tag:kitchen",
        name="厨房",
        aliases=(
            "厨房",
            "廚房",
            "キッチン",
            "kitchen",
        ),
    ),
    TagDefinition(
        id="tag:car",
        name="车震",
        aliases=(
            "车震",
            "車震",
            "車内",
            "car",
            "car sex",
        ),
    ),
    TagDefinition(
        id="tag:public",
        name="露出",
        aliases=(
            "露出",
            "公衆",
            "public",
            "公共",
            "公众野战",
            "公眾野戰",
            "野外・露出",
        ),
    ),
    TagDefinition(
        id="tag:outdoor",
        name="户外",
        aliases=(
            "户外",
            "戶外",
            "野外",
            "outdoor",
            "outdoors",
        ),
    ),
    TagDefinition(
        id="tag:webcam",
        name="摄像头",
        aliases=(
            "摄像头",
            "攝像頭",
            "webcam",
            "cam",
        ),
    ),
    TagDefinition(
        id="tag:hentai",
        name="里番",
        aliases=(
            "里番",
            "裏番",
            "hentai",
            "色情日漫",
        ),
    ),
    TagDefinition(
        id="tag:anime",
        name="动漫",
        aliases=(
            "动漫",
            "動漫",
            "アニメ",
            "anime",
            "cartoon",
            "卡通",
        ),
    ),
    TagDefinition(
        id="tag:pregnant",
        name="孕妇",
        aliases=(
            "孕妇",
            "孕婦",
            "妊婦",
            "pregnant",
        ),
    ),
    TagDefinition(
        id="tag:lactation",
        name="母乳",
        aliases=(
            "母乳",
            "授乳",
            "lactation",
            "lactating",
            "breastfeeding",
        ),
    ),
    TagDefinition(
        id="tag:tattoo",
        name="纹身",
        aliases=(
            "纹身",
            "紋身",
            "タトゥー",
            "tattoo",
            "tattoos",
            "纹身女",
            "紋身女",
        ),
    ),
    TagDefinition(
        id="tag:piercing",
        name="穿孔",
        aliases=(
            "穿孔",
            "ピアス",
            "piercing",
            "piercings",
        ),
    ),
    TagDefinition(
        id="tag:virgin",
        name="童贞",
        aliases=(
            "童贞",
            "童貞",
            "virgin",
        ),
    ),
    TagDefinition(
        id="tag:female-submissive",
        name="M女",
        aliases=(
            "M女",
            "masochist",
            "female masochist",
            "M-jo",
            "m-jo",
        ),
    ),
    TagDefinition(
        id="tag:male-submissive",
        name="M男",
        aliases=(
            "M男",
            "男M",
            "male masochist",
        ),
    ),
    TagDefinition(
        id="tag:gyaru",
        name="辣妹",
        aliases=(
            "辣妹",
            "ギャル",
            "gal",
            "gyaru",
        ),
    ),
    TagDefinition(
        id="tag:korean",
        name="韩国",
        aliases=(
            "韩国",
            "韓國",
            "韩国人",
            "韓國人",
        ),
    ),
    TagDefinition(
        id="tag:roleplay",
        name="角色扮演",
        aliases=(
            "角色扮演",
            "roleplay",
            "role play",
            "role-play",
        ),
    ),
    TagDefinition(
        id="tag:double-penetration",
        name="双龙",
        aliases=(
            "双龙",
            "雙龍",
            "双龙入洞",
            "雙龍入洞",
            "double penetration",
            "dp",
            "DP",
        ),
    ),

    TagDefinition(
        id="tag:ltx",
        name="LTX",
        aliases=(
            "LTX",
            "ltx",
            "中文字幕",
            "中字",
        ),
    ),
    TagDefinition(
        id="tag:digital-mosaic",
        name="数字马赛克",
        aliases=(
            "数字马赛克",
            "數字馬賽克",
            "Digital Mosaic",
            "digital mosaic",
        ),
    ),
    TagDefinition(
        id="tag:featured-actress",
        name="主打女优",
        aliases=(
            "主打女优",
            "主打女優",
            "精选女优",
            "精選女優",
            "Featured Actress",
            "featured actress",
        ),
    ),
    TagDefinition(
        id="tag:minimal-mosaic",
        name="薄马赛克",
        aliases=(
            "薄马赛克",
            "薄馬賽克",
            "Minimal Mosaic",
            "minimal mosaic",
        ),
    ),
    TagDefinition(
        id="tag:leaked",
        name="流出",
        aliases=(
            "流出",
            "leak",
            "leaked",
        ),
    ),
    TagDefinition(
        id="tag:reluctant",
        name="不情愿",
        aliases=(
            "不情愿",
            "不情願",
            "Reluctant",
            "reluctant",
        ),
    ),
    TagDefinition(
        id="tag:training",
        name="调教",
        aliases=(
            "调教",
            "調教",
            "Training",
            "training",
        ),
    ),
    TagDefinition(
        id="tag:no-condom",
        name="无套",
        aliases=(
            "无套",
            "無套",
            "No Condom",
            "no condom",
        ),
    ),
    TagDefinition(
        id="tag:cunnilingus",
        name="舔阴",
        aliases=(
            "舔阴",
            "舔陰",
            "クンニ",
            "Pussy Licking",
            "pussy licking",
        ),
    ),
    TagDefinition(
        id="tag:fingering",
        name="手指",
        aliases=(
            "手指",
            "指マン",
            "Fingering",
            "fingering",
        ),
    ),
    TagDefinition(
        id="tag:big-cock",
        name="巨根",
        aliases=(
            "巨根",
            "デカチン",
            "デカチン・巨根",
            "Big Dick",
            "big dick",
            "Huge Dick - Large Dick",
            "huge dick - large dick",
        ),
    ),
    TagDefinition(
        id="tag:voyeur",
        name="偷拍",
        aliases=(
            "偷拍",
            "盗撮",
            "盗撮・のぞき",
            "のぞき",
        ),
    ),
    TagDefinition(
        id="tag:upskirt",
        name="走光",
        aliases=(
            "走光",
            "パンチラ",
        ),
    ),
    TagDefinition(
        id="tag:kimono",
        name="和服",
        aliases=(
            "和服",
            "浴衣",
            "和服・浴衣",
        ),
    ),
    TagDefinition(
        id="tag:immediate-sex",
        name="即插",
        aliases=(
            "即插",
            "即ハメ",
        ),
    ),
    TagDefinition(
        id="tag:face-revealed",
        name="露脸",
        aliases=(
            "露脸",
            "露臉",
            "Face Revealed",
            "face revealed",
        ),
    ),
    TagDefinition(
        id="tag:urination",
        name="放尿",
        aliases=(
            "放尿",
            "お漏らし",
            "放尿・お漏らし",
        ),
    ),
    TagDefinition(
        id="tag:hostess",
        name="风俗娘",
        aliases=(
            "风俗娘",
            "風俗娘",
            "キャバ嬢・風俗嬢",
            "キャバ嬢",
            "Club Hostess & Sex Worker",
            "club hostess & sex worker",
        ),
    ),
    TagDefinition(
        id="tag:esthetic",
        name="美容按摩",
        aliases=(
            "美容按摩",
            "エステ",
        ),
    ),
    TagDefinition(
        id="tag:planning",
        name="企划",
        aliases=(
            "企划",
            "企劃",
            "企画",
        ),
    ),
    TagDefinition(
        id="tag:original-collab",
        name="原作联名",
        aliases=(
            "原作联名",
            "原作聯名",
            "原作コラボ",
            "Original Collaboration",
            "original collaboration",
        ),
    ),
    TagDefinition(
        id="tag:image-video",
        name="形象视频",
        aliases=(
            "形象视频",
            "形象視頻",
            "イメージビデオ",
        ),
    ),
    TagDefinition(
        id="tag:documentary-style",
        name="纪录风格",
        aliases=(
            "纪录风格",
            "紀錄風格",
            "ドキュメンタリー",
            "Documentary",
            "documentary",
        ),
    ),
    TagDefinition(
        id="tag:pornstar",
        name="女优",
        aliases=(
            "女优",
            "女優",
            "AV女優",
            "Pornstar",
            "pornstar",
            "Female Porn Star",
            "female porn star",
        ),
    ),
    TagDefinition(
        id="tag:mother",
        name="妈妈",
        aliases=(
            "妈妈",
            "媽媽",
            "お母さん",
        ),
    ),
    TagDefinition(
        id="tag:caucasian",
        name="白人",
        aliases=(
            "白人",
            "Caucasian",
            "caucasian",
        ),
    ),
    TagDefinition(
        id="tag:taiwan",
        name="台湾",
        aliases=(
            "台湾",
            "台灣",
            "Taiwan",
            "taiwan",
        ),
    ),
    TagDefinition(
        id="tag:groping",
        name="痴汉",
        aliases=(
            "痴汉",
            "癡漢",
            "Groping",
            "groping",
        ),
    ),
    TagDefinition(
        id="tag:rimming",
        name="舔肛",
        aliases=(
            "舔肛",
            "Rimming",
            "rimming",
        ),
    ),
    TagDefinition(
        id="tag:tall",
        name="高挑",
        aliases=(
            "高挑",
            "Tall Girl",
            "tall girl",
        ),
    ),
    TagDefinition(
        id="tag:long-hair",
        name="长发",
        aliases=(
            "长发",
            "長髮",
            "Long Hair",
            "long hair",
        ),
    ),
    TagDefinition(
        id="tag:nanpa",
        name="搭讪",
        aliases=(
            "搭讪",
            "搭訕",
            "ナンパ",
        ),
    ),
    TagDefinition(
        id="tag:rough",
        name="粗暴",
        aliases=(
            "粗暴",
            "Rough",
            "rough",
        ),
    ),
    TagDefinition(
        id="tag:sixtynine",
        name="六九式",
        aliases=(
            "六九式",
            "69",
            "シックスナイン",
        ),
    ),
    TagDefinition(
        id="tag:prank",
        name="恶作剧",
        aliases=(
            "恶作剧",
            "惡作劇",
            "イタズラ",
        ),
    ),
    TagDefinition(
        id="tag:close-up",
        name="局部特写",
        aliases=(
            "局部特写",
            "局部特寫",
            "局部アップ",
        ),
    ),
    TagDefinition(
        id="tag:date",
        name="约会",
        aliases=(
            "约会",
            "約會",
            "デート",
        ),
    ),
    TagDefinition(
        id="tag:bodycon",
        name="紧身衣",
        aliases=(
            "紧身衣",
            "緊身衣",
            "ボディコン",
        ),
    ),
    TagDefinition(
        id="tag:swimwear",
        name="竞泳泳装",
        aliases=(
            "竞泳泳装",
            "競泳泳裝",
            "競泳・スクール水着",
            "スクール水着",
        ),
    ),
    TagDefinition(
        id="tag:substance",
        name="药物",
        aliases=(
            "药物",
            "藥物",
            "ドラッグ",
            "Substance Use",
            "substance use",
        ),
    ),
    TagDefinition(
        id="tag:natural-tits",
        name="天然胸",
        aliases=(
            "天然胸",
            "Natural Tits",
            "natural tits",
        ),
    ),
    TagDefinition(
        id="tag:american",
        name="美国人",
        aliases=(
            "美国人",
            "美國人",
            "American",
            "american",
        ),
    ),
    TagDefinition(
        id="tag:masturbation-support",
        name="辅助自慰",
        aliases=(
            "辅助自慰",
            "輔助自慰",
            "オナサポ",
        ),
    ),

    TagDefinition(id="tag:long-duration", name="长篇", aliases=("长篇",)),
)

TAG_REGISTRY = TagRegistry(TAG_DEFINITIONS)
