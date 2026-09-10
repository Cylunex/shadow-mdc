from scripts.cleanup_non_jav_mis_tagged_actors import classify_removal


def test_classify_keeps_n_prefix_people_and_borderline() -> None:
    for name in (
        "Naimi奶咪",
        "Nana Taipei",
        "Natasha Nice",
        "蜜桃酱",
        "璇璇SWAG",
        "雪碧SWAG",
        "HongKongDoll",
    ):
        assert classify_removal(name) is None, name


def test_classify_removes_studios_and_fuzzy() -> None:
    assert classify_removal("麻豆传媒") == "studio_label"
    assert classify_removal("91探花") == "studio_label"
    assert classify_removal("北京探花") == "studio_label"
    assert classify_removal("约炮少妇") == "studio_label"
    assert classify_removal("Amateur") == "fuzzy"
    assert classify_removal("Unknown Guy") == "fuzzy"
