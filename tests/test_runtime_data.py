"""Reference data stays external and remains optional, validated and library scoped."""

import gzip
import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from scripts.migrate_reference_data import legacy_rules, migrate
from shadow_mdc.actress_aliases import get_actress_name_map
from shadow_mdc.api import app
from shadow_mdc.classifications import classification_definitions, classification_dimensions
from shadow_mdc.collection_names import detect_collection_kind
from shadow_mdc.dmm_ids import content_id_candidates, known_content_id_prefixes
from shadow_mdc.enums import CollectionKind
from shadow_mdc.services.tag_dictionary import TagDictionaryStore
from shadow_mdc.tag_models import seed_document
from shadow_mdc.tags import canonicalize_tag


def select_library(monkeypatch: pytest.MonkeyPatch, path: Path) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(path))
    monkeypatch.setenv("SHADOW_MDC_REFERENCE_DATA_DIR", str(path / "reference"))
    monkeypatch.setenv("SHADOW_MDC_DATABASE_URL", f"sqlite:///{path / 'catalog.db'}")
    monkeypatch.setenv("SHADOW_MDC_REDIS_URL", "")


def write_json(path: Path, value: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def test_empty_library_starts_without_reference_files(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_library(monkeypatch, tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/health").status_code == 200
        dimensions = client.get("/api/catalog/classifications").json()["dimensions"]
        assert len(dimensions) == 8  # Seven schema dimensions plus derived decades.
        assert all(dimension["values"] for dimension in dimensions[:7])
        assert app.state.runtime.tag_dictionary.load().terms == []
    assert len(get_actress_name_map()) == 0
    assert canonicalize_tag("Unregistered source label") == "Unregistered source label"
    assert content_id_candidates("DEMO-001")  # Generic ID expansion needs no prefix table.


def test_external_datasets_switch_with_library_and_reload_after_replacement(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for directory, name in (("first", "First"), ("second", "Second")):
        path = tmp_path / directory
        write_json(
            path / "reference/tag-dictionary.seed.json",
            {
                "terms": [{"id": "tag:fixture", "name": name, "aliases": ["Alias"]}],
                "rules": {},
            },
        )
        write_json(path / "reference/catalog-rules.json", {"platform_names": [name.casefold()]})
        (path / "reference/actress_name_map.json.gz").write_bytes(
            gzip.compress(json.dumps({"fixture": name}).encode()),
        )
        (path / "reference/dmm_content_id_prefixes.json.gz").write_bytes(
            gzip.compress(json.dumps({"demo": [name]}).encode()),
        )
        select_library(monkeypatch, path)
        assert canonicalize_tag("Alias") == name
        assert get_actress_name_map().resolve_japanese("Fixture") == name
        assert known_content_id_prefixes("demo") == (name,)
        assert detect_collection_kind(name) is CollectionKind.PLATFORM
        assert detect_collection_kind("unlisted") is CollectionKind.STUDIO
    runtime = tmp_path / "second/tag-dictionary.json"
    write_json(runtime, {"terms": [{"id": "tag:fixture", "name": "Persisted"}], "rules": {}})
    assert canonicalize_tag("tag:fixture") == "Persisted"
    replacement = runtime.with_suffix(".replacement")
    write_json(replacement, {"terms": [{"id": "tag:fixture", "name": "Replaced"}], "rules": {}})
    replacement.replace(runtime)
    assert canonicalize_tag("tag:fixture") == "Replaced"


def test_existing_dictionary_does_not_require_a_valid_seed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_library(monkeypatch, tmp_path)
    store = TagDictionaryStore(tmp_path / "tag-dictionary.json")
    store.initialize()
    store.create("Saved", (), False)
    write_json(tmp_path / "reference/tag-dictionary.seed.json", {"invalid": "seed"})
    store.initialize()
    assert store.load().terms[0].name == "Saved"
    with pytest.raises(ValidationError):
        seed_document()


def test_classification_data_is_loaded_after_import_and_keeps_structural_fallbacks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    select_library(monkeypatch, tmp_path)
    dimensions = classification_dimensions()
    write_json(
        tmp_path / "reference/classifications.seed.json",
        {
            "dimensions": [{"id": item.id, "name": "External", "description": ""} for item in dimensions],
            "terms": [{"id": "genres:fixture", "dimension": "genres", "name": "Fixture"}],
        },
    )
    assert classification_dimensions()[0].name == "External"
    identities = {term.id for term in classification_definitions()}
    assert {"genres:fixture", "genres:unknown", "contents:jav"}.issubset(identities)
    write_json(tmp_path / "reference/classifications.seed.json", {"dimensions": [], "terms": []})
    with pytest.raises(ValidationError, match="seven supported dimensions"):
        classification_dimensions()


def test_legacy_migration_copies_data_without_executing_code_or_overwriting_edits(tmp_path: Path) -> None:
    legacy = tmp_path / "legacy"
    script = legacy / "src/shadow_mdc/classification.py"
    script.parent.mkdir(parents=True)
    script.write_text(
        "raise RuntimeError('legacy modules must not run')\n"
        "_JAPAN_MARKERS = ('fixture',)\n"
        "_CONTROL_NAMES = frozenset({'control', *_JAPAN_MARKERS})\n",
    )
    data = script.parent / "data/actress_name_map.json.gz"
    data.parent.mkdir()
    data.write_bytes(gzip.compress(b'{"fixture": "Name"}'))
    output = tmp_path / "reference"
    assert set(migrate(legacy, output)) == {"actress_name_map.json.gz", "catalog-rules.json"}
    assert (output / data.name).read_bytes() == data.read_bytes()
    assert (output / data.name).stat().st_mode & 0o077 == 0
    rules = json.loads((output / "catalog-rules.json").read_text())
    assert rules["control_names"] == ["control", "fixture"]
    (output / "catalog-rules.json").write_text('{"keep": "user edit"}')
    assert migrate(legacy, output) == []
    assert json.loads((output / "catalog-rules.json").read_text()) == {"keep": "user edit"}
    script.write_text("_JAPAN_MARKERS = __import__('os').getcwd()\n")
    with pytest.raises(ValueError, match="unsupported legacy data expression"):
        legacy_rules(legacy)
