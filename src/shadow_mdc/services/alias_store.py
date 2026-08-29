import json
from pathlib import Path

from ..identity import IdentityAliasRules


class IdentityAliasStore:
    def __init__(self, path: Path):
        self._path = path

    def load(self) -> IdentityAliasRules:
        if not self._path.is_file():
            return default_alias_rules()
        try:
            payload = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read identity alias rules: {exc}") from exc
        return IdentityAliasRules.model_validate(payload)

    def save(self, rules: IdentityAliasRules) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._path.with_suffix(f"{self._path.suffix}.tmp")
        temporary.write_text(
            json.dumps(rules.model_dump(mode="json"), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._path)


def default_alias_rules() -> IdentityAliasRules:
    return IdentityAliasRules(
        studios={
            "杏吧传媒": "杏吧传媒",
            "杏吧原创": "杏吧传媒",
            "麻豆传媒": "麻豆传媒",
        },
        series={
            "小宝探花": "小宝探花",
            "渣男探花": "渣男探花",
            "文轩探花": "文轩探花",
            "步宾探花": "步宾探花",
            "七天探花": "七天探花",
        },
    )
