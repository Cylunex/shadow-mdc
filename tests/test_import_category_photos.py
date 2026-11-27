import hashlib
import json
from pathlib import Path

import pytest
from PIL import Image

from scripts.import_category_photos import import_photos
from shadow_mdc.tags import resolve_tag


def test_fill_preserves_existing_manifest_and_validates_before_publish(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SHADOW_MDC_DATA_DIR", str(tmp_path))
    covers = tmp_path / "category-covers"
    covers.mkdir()
    manifest = covers / "manifest.json"
    legacy = {"slug": "room", "label": "浴室", "aliases": ["浴場"], "custom": True}
    manifest.write_text(json.dumps({"categories": [legacy], "note": "keep"}))
    original = manifest.read_bytes()
    images = tmp_path / "images"
    images.mkdir()
    photo = images / "photo.jpg"
    Image.new("RGB", (100, 80), "teal").save(photo)
    tag = resolve_tag("浴室")
    assert tag is not None
    entry = dict(
        tag_id=tag.id,
        label=tag.name,
        slug="room",
        image_file=photo.name,
        sha256=hashlib.sha256(photo.read_bytes()).hexdigest(),
        source="fixture",
        source_title="Room",
        source_url="https://example.com/room",
        artist="Example",
        license="CC0",
        license_url="https://example.com/license",
    )
    pack = tmp_path / "pack.json"
    pack.write_text(json.dumps([entry]))
    assert import_photos(pack) == {"planned": 1}
    assert manifest.read_bytes() == original
    other = resolve_tag("护士")
    assert other is not None
    pack.write_text(
        json.dumps([entry, {**entry, "tag_id": other.id, "label": other.name, "sha256": "0" * 64}])
    )
    with pytest.raises(ValueError, match="Checksum"):
        import_photos(pack, apply=True)
    assert list(covers.iterdir()) == [manifest]
    assert manifest.read_bytes() == original
    pack.write_text(json.dumps([entry]))
    assert import_photos(pack, apply=True) == {"planned": 1, "added": 1}
    final = manifest.read_bytes()
    result = json.loads(final)
    assert result["note"] == "keep"
    assert len(result["categories"]) == 1
    saved = result["categories"][0]
    assert all(saved[key] == value for key, value in legacy.items())
    assert (covers / saved["image_file"]).read_bytes() == photo.read_bytes()
    assert import_photos(pack, apply=True) == {"already_covered": 1}
    assert manifest.read_bytes() == final
