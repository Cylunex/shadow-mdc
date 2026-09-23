from shadow_mdc.services.actress_names import ActressNameMap, get_actress_name_map, normalize_actress_key


def test_normalize_actress_key_strips_spaces() -> None:
    assert normalize_actress_key(" Ai  Kano ") == normalize_actress_key("AiKano")


def test_bundled_map_resolves_common_romaji() -> None:
    mapping = get_actress_name_map()
    assert len(mapping) > 1000
    assert mapping.resolve_japanese("Ai Kano") == "叶愛"
    expanded = mapping.expand_candidates(["Ai Kano"])
    assert expanded[0] == "叶愛"
    assert "Ai Kano" in expanded
    assert "Kano Ai" in expanded


def test_expand_candidates_dedupes() -> None:
    mapping = ActressNameMap.from_mapping({"arina hashimoto": "橋本ありな"})
    assert mapping.expand_candidates(["橋本ありな", "Arina Hashimoto", "arina hashimoto"]) == [
        "橋本ありな",
        "Arina Hashimoto",
        "Hashimoto Arina",
    ]
