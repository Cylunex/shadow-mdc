"""Preserve legacy bundled datasets before switching to a code-only release."""

from __future__ import annotations

import argparse
import ast
import os
import tempfile
from pathlib import Path

from shadow_mdc.reference_models import CatalogRules

type LiteralData = str | int | float | bool | list[LiteralData] | dict[str, LiteralData] | None

# These are source field locations, never catalog records or vocabulary defaults.
LEGACY_FIELDS: dict[str, dict[str, str]] = {
    "src/shadow_mdc/collection_names.py": {"KNOWN_PLATFORM_NAMES": "platform_names"},
    "src/shadow_mdc/classification.py": {
        f"_{name.upper()}": name
        for name in (
            "china_markers",
            "japan_markers",
            "korea_markers",
            "europe_markers",
            "animation_markers",
            "other_markers",
            "control_names",
        )
    },
    "src/shadow_mdc/services/studio_guard.py": {
        "EXPLICIT_STUDIO_LABELS": "explicit_studio_labels",
        "EXPLICIT_FUZZY_NAMES": "explicit_fuzzy_names",
        "KEEP_BORDERLINE": "keep_borderline",
        "_PREFIX_SERIES": "prefix_series",
        "_SUFFIX_SERIES": "suffix_series",
        "_STUDIO_TOKENS": "studio_tokens",
    },
    "scripts/cleanup_non_jav_mis_tagged_actors.py": {"CONFIDENT_MERGES": "confident_merges"},
    "scripts/enrich_non_jav_incomplete.py": {
        "_STUDIO_SITE_HINTS": "studio_site_hints",
        "_STUDIO_SITE_EXTRA": "studio_site_extra",
        "_KNOWN_CREATOR_SITES": "known_creator_sites",
        "_CODE_PREFIX_SITE": "code_prefix_site",
    },
    "scripts/import_nv_pu_sa_bloggers.py": {
        "ARCHIVE_URLS": "archive_urls",
        "_KNOWN_SITES": "blogger_sites",
        "_PREFERRED_NAMES": "blogger_names",
    },
    "scripts/refresh_chinese_media.py": {
        "_STUDIO_NAME_MARKERS": "studio_name_markers",
        "_CHINESE_SITES": "chinese_sites",
        "CURATED_PLATFORM_SEEDS": "platform_seeds",
    },
    "scripts/seed_non_jav_placeholders.py": {
        "EXTRA_ACTORS": "extra_actors",
        "CHINA_EXTRA": "china_extra",
        "extra_alias_map": "extra_aliases",
    },
}


def literal(node: ast.expr, known: dict[str, LiteralData]) -> LiteralData:
    """Read only literal containers and prior names; never execute legacy source code."""
    if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float, bool, type(None))):
        return node.value
    if isinstance(node, ast.Name):
        return known[node.id]
    if isinstance(node, ast.List | ast.Tuple | ast.Set):
        result: list[LiteralData] = []
        for item in node.elts:
            if isinstance(item, ast.Starred):
                values = literal(item.value, known)
                if not isinstance(values, list):
                    raise ValueError("legacy spread must contain a literal list")
                result.extend(values)
            else:
                result.append(literal(item, known))
        return result
    if isinstance(node, ast.Dict):
        result_dict: dict[str, LiteralData] = {}
        for key, value in zip(node.keys, node.values, strict=True):
            if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
                raise ValueError("legacy object keys must be literal strings")
            result_dict[key.value] = literal(value, known)
        return result_dict
    if (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "frozenset"
        and len(node.args) == 1
        and not node.keywords
    ):
        return literal(node.args[0], known)
    raise ValueError("unsupported legacy data expression")


def legacy_rules(root: Path) -> CatalogRules:
    values: dict[str, LiteralData] = {}
    for filename, fields in LEGACY_FIELDS.items():
        path = root / filename
        if not path.is_file():
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"))
        known: dict[str, LiteralData] = {}
        assignments = sorted(
            (node for node in ast.walk(tree) if isinstance(node, ast.Assign | ast.AnnAssign)),
            key=lambda node: node.lineno,
        )
        for node in assignments:
            target = node.target if isinstance(node, ast.AnnAssign) else node.targets[0]
            if not isinstance(target, ast.Name) or target.id not in fields or node.value is None:
                continue
            # The later EXTRA_ACTORS concatenation is already represented by its two lists.
            if isinstance(node.value, ast.BinOp):
                continue
            value = literal(node.value, known)
            known[target.id] = value
            values[fields[target.id]] = value
        # Older versions kept these small lists inline in generator/for expressions.
        for expression in ast.walk(tree):
            if (
                filename.endswith("collection_names.py")
                and isinstance(expression, ast.comprehension)
                and isinstance(expression.target, ast.Name)
                and expression.target.id == "stem"
            ):
                values["platform_stems"] = literal(expression.iter, known)
            if (
                filename.endswith("refresh_chinese_media.py")
                and isinstance(expression, ast.For)
                and isinstance(expression.target, ast.Tuple)
                and isinstance(expression.target.elts[0], ast.Name)
            ):
                field = {"actor_name": "platform_actors", "blogger": "blogger_searches"}.get(
                    expression.target.elts[0].id,
                )
                if field:
                    values[field] = literal(expression.iter, known)
    return CatalogRules.model_validate(values)


def write_missing(path: Path, raw: bytes) -> bool:
    """Publish complete private files without replacing existing user data."""
    descriptor, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return False
        return True
    finally:
        Path(temporary).unlink()


def migrate(root: Path, output: Path) -> list[str]:
    output.mkdir(parents=True, exist_ok=True, mode=0o700)
    written: list[str] = []
    for name in (
        "actress_name_map.json.gz",
        "dmm_content_id_prefixes.json.gz",
        "tag-dictionary.seed.json",
        "classifications.seed.json",
    ):
        source = root / "src/shadow_mdc/data" / name
        if (
            source.is_file()
            and not (output / name).exists()
            and write_missing(output / name, source.read_bytes())
        ):
            written.append(name)
    rules = output / "catalog-rules.json"
    if not rules.exists() and write_missing(
        rules, (legacy_rules(root).model_dump_json(indent=2) + "\n").encode()
    ):
        written.append(rules.name)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--legacy-root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    written = migrate(args.legacy_root, args.output)
    print(f"Preserved {len(written)} external reference files; existing data was retained.")


if __name__ == "__main__":
    main()
