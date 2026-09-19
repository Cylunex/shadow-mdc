from shadow_mdc.tags import (
    canonicalize_tag,
    display_chips,
    facet_tags,
    is_noise_tag,
    normalize_tags,
    work_matches_tags,
)


def test_normalize_synonyms_costume_and_body() -> None:
    assert canonicalize_tag("黒ストッキング") == "黑丝"
    assert canonicalize_tag("black pantyhose") == "黑丝"
    assert canonicalize_tag("パンスト・タイツ") == "丝袜"
    assert canonicalize_tag("pantyhose") == "丝袜"
    assert canonicalize_tag("ハイヒール") == "高跟鞋"
    assert canonicalize_tag("high heels") == "高跟鞋"
    assert canonicalize_tag("巨乳") == "巨乳"
    assert canonicalize_tag("大奶") == "巨乳"
    assert canonicalize_tag("美乳") == "巨乳"


def test_normalize_act_and_role_synonyms() -> None:
    assert canonicalize_tag("中出し") == "中出"
    assert canonicalize_tag("内射") == "中出"
    assert canonicalize_tag("creampie") == "中出"
    assert canonicalize_tag("フェラ") == "口交"
    assert canonicalize_tag("顔射") == "颜射"
    assert canonicalize_tag("看護婦・ナース") == "护士"
    assert canonicalize_tag("女教師") == "老师"
    assert canonicalize_tag("メイド") == "女仆"
    assert canonicalize_tag("単体作品") == "单体作品"
    assert canonicalize_tag("レズビアン") == "女同"


def test_normalize_tags_dedupes_filters_noise_and_orders() -> None:
    result = normalize_tags(
        [
            "jav-yearly-seed",
            "non-jav-seed",
            "theporndb",
            "chinese",
            "swaglivepeachmedia",
            "中出し",
            "巨乳",
            "大奶",
            "フェラ",
            "黒ストッキング",
            "ハイビジョン",
            "単体作品",
        ]
    )
    assert "non-jav-seed" not in result
    assert "theporndb" not in result
    assert "chinese" not in result
    assert result.count("巨乳") == 1
    assert result == normalize_tags(result)  # deterministic
    assert result.index("黑丝") < result.index("高清")
    assert "中出" in result and "口交" in result and "单体作品" in result


def test_is_noise_tag() -> None:
    assert is_noise_tag("jav-top-2024")
    assert is_noise_tag("year-2023")
    assert is_noise_tag("daily-hot-2026-09-13")
    assert is_noise_tag("swag")
    assert not is_noise_tag("巨乳")
    assert not is_noise_tag("制服")


def test_work_matches_tags_and_semantics() -> None:
    tags = ["中出し", "巨乳", "フェラ", "jav-yearly-seed"]
    assert work_matches_tags(tags, ["中出"])
    assert work_matches_tags(tags, ["大奶", "口交"])  # synonyms + AND
    assert not work_matches_tags(tags, ["中出", "黑丝"])
    assert work_matches_tags(tags, [])
    # Noise-only filters canonicalize to nothing, so they do not restrict results.
    assert work_matches_tags(tags, ["jav-yearly-seed"]) is True


def test_facet_tags_excludes_noise_and_keeps_common() -> None:
    facets = facet_tags(
        [
            ["中出し", "巨乳", "jav-yearly-seed"],
            ["中出し", "制服", "theporndb"],
            ["パンスト・タイツ", "高跟鞋"],
            ["non-jav-seed", "chinese"],
        ],
        limit=10,
    )
    names = [name for name, _ in facets]
    assert "中出" in names
    assert "巨乳" in names
    assert "丝袜" in names
    assert "jav-yearly-seed" not in names
    assert "theporndb" not in names
    assert "chinese" not in names


def test_display_chips_prioritize_normalized_genres() -> None:
    chips = display_chips(
        category="Japan",
        family="jav",
        studio="S1 NO.1 STYLE",
        series="シリーズA",
        tags=["中出し", "巨乳", "non-jav-seed", "フェラ"],
    )
    assert chips[0] in {"巨乳", "口交", "中出"}
    assert "non-jav-seed" not in chips
    assert "Japan" in chips
    assert "S1 NO.1 STYLE" in chips
    assert "jav" not in chips  # family jav skipped


def test_milf_split_from_hitodzuma() -> None:
    """MILF stays English; 人妻 stays JP married-woman; mature stays 熟女."""
    assert canonicalize_tag("milf") == "MILF"
    assert canonicalize_tag("MILF") == "MILF"
    assert canonicalize_tag("人妻") == "人妻"
    assert canonicalize_tag("人妻・主婦") == "人妻"
    assert canonicalize_tag("義母") == "人妻"
    assert canonicalize_tag("mature") == "熟女"
    assert canonicalize_tag("milf") != "人妻"


def test_pov_bbc_bdsm_kept_as_english_loanwords() -> None:
    assert canonicalize_tag("pov") == "POV"
    assert canonicalize_tag("POV") == "POV"
    assert canonicalize_tag("主観") == "POV"
    assert canonicalize_tag("主观") == "POV"
    assert canonicalize_tag("bbc") == "BBC"
    assert canonicalize_tag("bbw") == "BBW"
    assert canonicalize_tag("BDSM") == "BDSM"
    assert canonicalize_tag("SM") == "BDSM"
    assert canonicalize_tag("interracial") == "Interracial"
    assert canonicalize_tag("latina") == "Latina"
    assert canonicalize_tag("ebony") == "Ebony"
    assert canonicalize_tag("blonde") == "Blonde"
    assert canonicalize_tag("brunette") == "Brunette"
    assert canonicalize_tag("redhead") == "Redhead"


def test_uniform_vs_cosplay_split() -> None:
    assert canonicalize_tag("制服") == "制服"
    assert canonicalize_tag("uniform") == "制服"
    assert canonicalize_tag("コスプレ") == "Cosplay"
    assert canonicalize_tag("cosplay") == "Cosplay"
    assert canonicalize_tag("コスプレ") != "制服"


def test_xvideos_classic_body_act_cn_labels() -> None:
    assert canonicalize_tag("big-tits") == "巨乳"
    assert canonicalize_tag("big-boobs") == "巨乳"
    assert canonicalize_tag("big-ass") == "美尻"
    assert canonicalize_tag("petite") == "娇小"
    assert canonicalize_tag("skinny") == "苗条"
    assert canonicalize_tag("chubby") == "微胖"
    assert canonicalize_tag("hairy") == "多毛"
    assert canonicalize_tag("shaved") == "刮毛"
    assert canonicalize_tag("amateur") == "素人"
    assert canonicalize_tag("asian") == "亚洲"
    assert canonicalize_tag("japanese") == "日本"
    assert canonicalize_tag("blowjob") == "口交"
    assert canonicalize_tag("deepthroat") == "深喉"
    assert canonicalize_tag("イラマチオ") == "深喉"
    assert canonicalize_tag("swallow") == "吞精"
    assert canonicalize_tag("ごっくん") == "吞精"
    assert canonicalize_tag("cumshot") == "射精"
    assert canonicalize_tag("creampie") == "中出"
    assert canonicalize_tag("facial") == "颜射"
    assert canonicalize_tag("squirting") == "潮吹"
    assert canonicalize_tag("handjob") == "手交"
    assert canonicalize_tag("footjob") == "足交"
    assert canonicalize_tag("doggystyle") == "后入"
    assert canonicalize_tag("cowgirl") == "骑乘"
    assert canonicalize_tag("骑乘位") == "骑乘"
    assert canonicalize_tag("reverse-cowgirl") == "反骑乘"
    assert canonicalize_tag("missionary") == "正常位"


def test_xvideos_classic_scene_role_fetish() -> None:
    assert canonicalize_tag("bondage") == "捆绑"
    assert canonicalize_tag("拘束") == "捆绑"
    assert canonicalize_tag("fetish") == "癖好"
    assert canonicalize_tag("hentai") == "里番"
    assert canonicalize_tag("anime") == "动漫"
    assert canonicalize_tag("compilation") == "合集"
    assert canonicalize_tag("webcam") == "摄像头"
    assert canonicalize_tag("pregnant") == "孕妇"
    assert canonicalize_tag("tattoo") == "纹身"
    assert canonicalize_tag("piercing") == "穿孔"
    assert canonicalize_tag("couple") == "情侣"
    assert canonicalize_tag("cuckold") == "NTR"
    assert canonicalize_tag("stepmom") == "继母"
    assert canonicalize_tag("office") == "OL"
    assert canonicalize_tag("secretary") == "OL"
    assert canonicalize_tag("solo") == "自慰"
    assert canonicalize_tag("toys") == "玩具"
    assert canonicalize_tag("dildo") == "玩具"
    assert canonicalize_tag("vibrator") == "玩具"
    assert canonicalize_tag("casting") == "试镜"
    assert canonicalize_tag("hotel") == "酒店"
    assert canonicalize_tag("beach") == "海滩"
    assert canonicalize_tag("shower") == "淋浴"
    assert canonicalize_tag("kitchen") == "厨房"
    assert canonicalize_tag("car") == "车震"
    assert canonicalize_tag("bathroom") == "浴室"
    assert canonicalize_tag("public") == "露出"
    assert canonicalize_tag("outdoor") == "户外"
    assert canonicalize_tag("lingerie") == "内衣"
    assert canonicalize_tag("fishnet") == "网袜"
    assert canonicalize_tag("stockings") == "丝袜"
    assert canonicalize_tag("pantyhose") == "丝袜"
    assert canonicalize_tag("high-heels") == "高跟鞋"
    assert canonicalize_tag("heels") == "高跟鞋"
    assert canonicalize_tag("threesome") == "多人"
    assert canonicalize_tag("gangbang") == "多人"
    assert canonicalize_tag("orgy") == "多人"
    assert canonicalize_tag("lesbian") == "女同"
    assert canonicalize_tag("massage") == "按摩"
    assert canonicalize_tag("shemale") == "Shemale"


def test_seed_chinese_korean_remain_blacklisted() -> None:
    assert canonicalize_tag("chinese") is None
    assert canonicalize_tag("korean") is None
    assert is_noise_tag("chinese")
    assert is_noise_tag("korean")
