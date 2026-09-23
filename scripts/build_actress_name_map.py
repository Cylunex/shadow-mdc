#!/usr/bin/env python3
"""Rebuild bundled actress_name_map.json.gz from peer refs (offline).

Sources (must exist under /workspace/refs or override via env):
  - Javinizer jvThumbs.csv (MIT)
  - JavSP actress_alias.json (GPL-3.0)
"""

from __future__ import annotations

import csv
import gzip
import json
import os
import re
import sys
import unicodedata
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REFS = Path(os.environ.get("SHADOW_MDC_REFS", "/workspace/refs"))
OUT = ROOT / "src" / "shadow_mdc" / "data" / "actress_name_map.json.gz"


def norm(value: str) -> str:
    cleaned = unicodedata.normalize("NFKC", value or "").casefold().strip()
    return re.sub(r"\s+", "", cleaned)


def main() -> int:
    thumbs = REFS / "Javinizer" / "src" / "Javinizer" / "jvThumbs.csv"
    javsp = REFS / "JavSP" / "data" / "actress_alias.json"
    if not thumbs.is_file():
        print(f"missing {thumbs}", file=sys.stderr)
        return 2
    if not javsp.is_file():
        print(f"missing {javsp}", file=sys.stderr)
        return 2

    jp_by_key: dict[str, str] = {}
    with thumbs.open(newline="", encoding="utf-8-sig") as handle:
        for row in csv.DictReader(handle):
            japanese = (row.get("JapaneseName") or "").strip()
            if not japanese:
                continue
            variants: set[str] = set()
            full = (row.get("FullName") or "").strip()
            last = (row.get("LastName") or "").strip()
            first = (row.get("FirstName") or "").strip()
            alias = (row.get("Alias") or "").strip()
            if full:
                variants.add(full)
            if last and first:
                variants.add(f"{last} {first}")
                variants.add(f"{first} {last}")
            if alias:
                for part in re.split(r"[|/;,]+", alias):
                    part = part.strip()
                    if part:
                        variants.add(part)
            japanese_key = norm(japanese)
            for variant in variants:
                key = norm(variant)
                if key and key != japanese_key:
                    jp_by_key.setdefault(key, japanese)

    payload = json.loads(javsp.read_text(encoding="utf-8"))
    for canon, aliases in payload.items():
        canon_key = norm(canon)
        for alias in aliases:
            key = norm(alias)
            if key and key != canon_key:
                jp_by_key.setdefault(key, canon)

    raw = json.dumps(jp_by_key, ensure_ascii=False, separators=(",", ":"), sort_keys=True).encode("utf-8")
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with gzip.open(OUT, "wb", compresslevel=9) as handle:
        handle.write(raw)
    print(f"wrote {OUT} entries={len(jp_by_key)} raw={len(raw)} gz={OUT.stat().st_size}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
