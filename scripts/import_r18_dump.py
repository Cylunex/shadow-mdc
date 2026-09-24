#!/usr/bin/env python3
"""Download (optional) and import the weekly r18.dev PostgreSQL dump into SQLite.

Keeps dump + SQLite under the data directory (NAS-only). Never commit these files.

Examples::

    PYTHONPATH=src .venv/bin/python scripts/import_r18_dump.py --dump /path/to.sql.gz
    PYTHONPATH=src .venv/bin/python scripts/import_r18_dump.py --refresh --proxy http://192.168.0.110:7893
"""

from __future__ import annotations

import argparse
import os
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.services.r18_dump import (  # noqa: E402
    PROVIDER_ID,
    R18DumpStore,
    import_dump_to_sqlite,
)

LATEST_URL = "https://r18.dev/dumps/latest"


def _dump_dir(settings: Settings) -> Path:
    path = settings.data_dir / "r18-dumps"
    path.mkdir(parents=True, exist_ok=True)
    return path


def _download_latest(target_dir: Path, *, proxy: str | None) -> Path:
    handlers: list[urllib.request.BaseHandler] = []
    if proxy:
        handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
    opener = urllib.request.build_opener(*handlers)
    print(f"Downloading {LATEST_URL} …", flush=True)
    with opener.open(LATEST_URL, timeout=600) as response:
        final_url = response.geturl()
        name = Path(final_url).name or "r18dev_dump_latest.sql.gz"
        if not name.endswith(".gz"):
            name = f"{name}.sql.gz"
        destination = target_dir / name
        temporary = destination.with_suffix(destination.suffix + ".part")
        total = 0
        with temporary.open("wb") as handle:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                handle.write(chunk)
                total += len(chunk)
                if total % (20 * 1024 * 1024) < 1024 * 1024:
                    print(f"  … {total / (1024 * 1024):.1f} MiB", flush=True)
        temporary.replace(destination)
    print(f"Saved {destination} ({destination.stat().st_size} bytes) from {final_url}")
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--dump", type=Path, default=None, help="Existing .sql.gz dump path")
    parser.add_argument("--sqlite", type=Path, default=None, help="Output SQLite path")
    parser.add_argument("--refresh", action="store_true", help="Download latest dump before import")
    parser.add_argument("--proxy", default=os.environ.get("https_proxy") or os.environ.get("HTTPS_PROXY"))
    parser.add_argument("--progress-every", type=int, default=50_000)
    arguments = parser.parse_args()

    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    dump_dir = _dump_dir(settings)
    dump_path = arguments.dump
    if arguments.refresh or dump_path is None:
        if dump_path is None and not arguments.refresh:
            existing = sorted(dump_dir.glob("*.sql.gz"))
            if existing:
                dump_path = existing[-1]
                print(f"Using existing dump {dump_path}")
            else:
                arguments.refresh = True
        if arguments.refresh:
            dump_path = _download_latest(dump_dir, proxy=arguments.proxy)
    assert dump_path is not None
    if not dump_path.is_file():
        print(f"dump not found: {dump_path}", file=sys.stderr)
        return 2

    sqlite_path = arguments.sqlite or (dump_dir / "r18_dump.db")
    print(f"Importing {dump_path} → {sqlite_path} (provider={PROVIDER_ID})")
    stats = import_dump_to_sqlite(dump_path, sqlite_path, progress_every=arguments.progress_every)
    print(f"Done: videos={stats.videos} actresses={stats.actresses} tables={stats.tables}")
    with R18DumpStore(sqlite_path) as store:
        print(f"Meta: {store.meta()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
