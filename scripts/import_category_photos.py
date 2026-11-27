"""Fill empty category covers from a reviewed pack of attributed JPEG photos.

Run with the target library selected via SHADOW_MDC_DATA_DIR. A pack is a JSON
list with the Photo fields below, accompanied by an images/ directory.
"""

from __future__ import annotations

import argparse
import fcntl
import hashlib
import json
import os
from collections import Counter
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Any

from PIL import Image
from pydantic import BaseModel, Field, TypeAdapter

from shadow_mdc.runtime_data import data_directories
from shadow_mdc.services.category_catalog import covers_dir, manifest_path
from shadow_mdc.tags import resolve_tag


class Photo(BaseModel):
    tag_id: str
    label: str
    slug: str
    image_file: str
    sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    source: str
    source_title: str
    source_url: str
    artist: str
    license: str
    license_url: str


def atomic_write(path: Path, content: bytes) -> None:
    with NamedTemporaryFile(dir=path.parent, delete=False) as tmp:
        temp = Path(tmp.name)
        try:
            tmp.write(content)
            tmp.flush()
            os.fsync(tmp.fileno())
            temp.chmod(0o644)
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)


def import_photos(pack: Path, *, apply: bool = False) -> dict[str, int]:
    photos = TypeAdapter(list[Photo]).validate_json(pack.read_bytes())
    data_dir = data_directories()[0]
    directory = covers_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = manifest_path(data_dir)
    counts: Counter[str] = Counter()
    # The directory inode stays stable across atomic manifest replacements.
    fd = os.open(directory, os.O_RDONLY)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        original = path.read_bytes() if path.exists() else None
        manifest: Any = json.loads(original) if original is not None else {"version": 1, "categories": []}
        rows: list[dict[str, Any]] = manifest if isinstance(manifest, list) else manifest["categories"]
        by_id: dict[str, dict[str, Any]] = {}
        for existing in rows:
            tag = resolve_tag(existing.get("tag_id") or existing["label"])
            if tag:
                by_id[tag.id] = existing
        slugs = {row.get("slug") for row in rows}
        assets: dict[Path, bytes] = {}
        for photo in photos:
            tag = resolve_tag(photo.tag_id)
            if not tag or tag.id != photo.tag_id or tag.name != photo.label:
                raise ValueError(f"Tag identity changed: {photo.tag_id}")
            row = by_id.get(tag.id)
            if row and (row.get("image_file") or row.get("image")):
                counts["already_covered"] += 1
                continue
            if Path(photo.image_file).name != photo.image_file:
                raise ValueError("Expected a filename inside images/")
            image = pack.parent / "images" / photo.image_file
            if image.is_symlink() or image.stat().st_size > 8 * 1024 * 1024:
                raise ValueError(f"Invalid photo: {image.name}")
            content = image.read_bytes()
            if hashlib.sha256(content).hexdigest() != photo.sha256:
                raise ValueError(f"Checksum mismatch: {image.name}")
            with Image.open(image) as decoded:
                if decoded.format != "JPEG" or min(decoded.size) < 64:
                    raise ValueError(f"Invalid JPEG: {image.name}")
                decoded.load()
            target = directory / f"photo-{photo.sha256}.jpg"
            if target.is_symlink() or (target.exists() and target.read_bytes() != content):
                raise ValueError(f"Conflicting asset: {target.name}")
            if not target.exists():
                assets[target] = content
            if row is None:
                if photo.slug in slugs:
                    raise ValueError(f"Conflicting slug: {photo.slug}")
                row = {"tag_id": tag.id, "label": tag.name, "slug": photo.slug}
                rows.append(row)
                by_id[tag.id] = row
                slugs.add(photo.slug)
            row.update(
                photo.model_dump(
                    include={"source", "source_title", "source_url", "artist", "license", "license_url"}
                ),
                image_file=target.name,
            )
            counts["planned"] += 1
        if apply and counts["planned"]:
            for target, content in assets.items():
                atomic_write(target, content)
            if (path.read_bytes() if path.exists() else None) != original:
                raise RuntimeError("Category manifest changed during import")
            atomic_write(path, (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())
            counts["added"] = counts["planned"]
        return dict(counts)
    finally:
        os.close(fd)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("pack", type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    print(json.dumps(import_photos(args.pack, apply=args.apply)))
