from shadow_mdc.enums import MediaCategory
from shadow_mdc.identity import IdentityAliasRules, build_identity_hints
from shadow_mdc.services.non_jav_actor_catalog import (
    enrich_non_jav_actor_aliases,
    parse_non_jav_actor_text,
)


def test_parse_non_jav_actor_text_deduplicates_and_keeps_metadata() -> None:
    catalog = parse_non_jav_actor_text(
        """
### **一、欧美热门女优**
Abella Danger,Abella Danger
Abella Danger,Abella Danger\uff08重复但高频\uff09
### **二、麻豆热门女优**
苏畅,苏畅
麻豆12金钗系列,CX系列女优
### **四、OnlyFans热门女优**
HongKongDoll,HongKongDoll,玩偶姐姐
Mia (Mia 💕),Mia
""",
        source="fixture.txt",
    )

    by_name = {actor.name: actor for actor in catalog.actors}
    assert set(by_name) == {"Abella Danger", "HongKongDoll", "Mia", "苏畅"}
    assert by_name["Abella Danger"].groups == ("western",)
    assert by_name["Abella Danger"].categories == (MediaCategory.EUROPE,)
    assert "玩偶姐姐" in by_name["HongKongDoll"].match_names
    assert by_name["Mia"].match_names == ()


def test_non_jav_catalog_enriches_local_hints_without_becoming_jav() -> None:
    catalog = parse_non_jav_actor_text(
        """
### **四、OnlyFans热门女优**
HongKongDoll,HongKongDoll,玩偶姐姐
""",
        source="fixture.txt",
    )
    rules = enrich_non_jav_actor_aliases(IdentityAliasRules(), catalog)

    hints = build_identity_hints(
        "V (1).strm",
        context_names=("玩偶姐姐", "国产"),
        alias_rules=rules,
    )

    assert hints.code is None
    assert hints.category is MediaCategory.CHINA
    assert hints.actors == ("HongKongDoll",)
