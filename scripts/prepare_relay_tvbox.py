#!/usr/bin/env python3
"""Prepare a private TVBox seed file from a Relay workspace export."""

import argparse
import json
import os
import tarfile
from pathlib import Path

from pydantic import BaseModel, Field, ValidationError

from shadow_mdc.services.tvbox_relay import RelayTransfer, prepare_transfer

MAX_MANIFEST = 32 * 1024 * 1024


class Entry(BaseModel):
    id: str = ""
    name: str = ""
    url: str = ""
    group: str = ""
    data: dict[str, object] | None = None


class Normalized(BaseModel):
    items: list[Entry] = Field(default_factory=list)


class Revision(BaseModel):
    normalized: Normalized


class Source(BaseModel):
    protocol: str
    enabled: bool
    activeRevision: str = ""
    stagedRevision: str = ""


class Record(BaseModel):
    id: str
    data: dict[str, object]


class Manifest(BaseModel):
    format: str
    tables: dict[str, list[Record]]


def read_manifest(path: Path) -> Manifest:
    if path.suffix == ".json":
        with path.open("rb") as stream:
            raw = stream.read(MAX_MANIFEST + 1)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive:
                if member.name != "manifest.json":
                    continue
                if member.size > MAX_MANIFEST or not member.isfile():
                    raise ValueError("invalid manifest")
                extracted = archive.extractfile(member)
                if extracted is None:
                    raise ValueError("missing manifest")
                with extracted:
                    raw = extracted.read(MAX_MANIFEST + 1)
                break
            else:
                raise ValueError("missing manifest")
    if len(raw) > MAX_MANIFEST:
        raise ValueError("manifest exceeds the size limit")
    manifest = Manifest.model_validate_json(raw)
    if manifest.format != "shadow-relay-data/v1":
        raise ValueError("unsupported Relay export")
    return manifest


def prepare(manifest: Manifest) -> tuple[dict[str, list[dict[str, str | int]]], dict[str, int]]:
    revisions = {record.id: record.data for record in manifest.tables["revisions"]}
    sites: dict[str, dict[str, str | int]] = {}
    counts = {"source_packs": 0, "staged": 0, "type_0": 0, "other_references": 0}
    for record in manifest.tables["sources"]:
        if record.data.get("protocol") != "tvbox":
            continue
        source = Source.model_validate(record.data)
        if not source.enabled or not source.activeRevision:
            continue
        counts["source_packs"] += 1
        counts["staged"] += bool(source.stagedRevision)
        revision = Revision.model_validate(revisions[source.activeRevision])
        for item in revision.normalized.items:
            if item.group != "site":
                counts["other_references"] += 1
                continue
            site_type = (item.data or {}).get("type")
            if site_type == 0:
                counts["type_0"] += 1
            if site_type != 1 or not item.url.startswith(("https://", "http://")):
                continue
            sites.setdefault(
                item.url,
                {
                    "key": item.id,
                    "name": item.name,
                    "type": 1,
                    "api": item.url,
                },
            )
    counts["type_1"] = len(sites)
    return {"sites": list(sites.values())}, counts


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("relay_export", type=Path, help="private v1 Relay archive or its manifest")
    parser.add_argument("output", type=Path, help="new private JSON file for MDC TVBox import")
    parser.add_argument("--takeover", action="store_true", help="include TVBox management state and history")
    args = parser.parse_args()
    try:
        manifest = read_manifest(args.relay_export)
        output: object
        if args.takeover:
            transfer = prepare_transfer(RelayTransfer.model_validate(manifest.model_dump()))
            output = transfer.model_dump()
            counts = {name: len(rows) for name, rows in transfer.tables.items()}
        else:
            config, counts = prepare(manifest)
            if not config["sites"]:
                raise ValueError("no compatible sites")
            output = config
        fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(output, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
    except (OSError, tarfile.TarError, ValidationError, ValueError, KeyError) as exc:
        parser.exit(1, f"Relay TVBox preparation failed: {type(exc).__name__}\n")
    print(json.dumps(counts, sort_keys=True))


if __name__ == "__main__":
    main()
