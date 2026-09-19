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
