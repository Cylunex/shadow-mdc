#!/usr/bin/env python3
"""Import OpenList-generated STRM scrapes into shadow-mdc {studio}/{CODE}/ layout.

Discovers title folders from a *local* SaveStrm mirror (fast), matches codes in
the shadow-mdc DB, parses OpenList ``/d`` URLs out of existing ``.strm`` bodies
(so we avoid a paced OpenList directory walk), and re-exports matched titles
into ``strm_output_root`` with ``movie.nfo`` / artwork / sidecar.

Unmatched / junk folders are counted and left alone. Optionally moves the local
mirror aside to ``_legacy_<name>_<stamp>/``.

Usage (on NAS)::

  cd /data/project/shadow-mdc/current
  SHADOW_MDC_DATA_DIR=/data/project/shadow-mdc/shared/data \\
  SHADOW_MDC_DATABASE_URL=sqlite:////data/project/shadow-mdc/shared/data/shadow-mdc.db \\
  PYTHONPATH=src .venv/bin/python scripts/rematerialize_openlist_strm_tree.py \\
    --local-root /media/Cloud/media/115_strm/Pending
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.repository import Database, Repository  # noqa: E402
from shadow_mdc.services.pan import PanService, RemoteVideo  # noqa: E402
from shadow_mdc.services.strm_export import (  # noqa: E402
    export_relative_dir,
    export_work,
    map_to_emby_path,
)

CODE_RE = re.compile(r"(?i)(?<![A-Z0-9])([A-Z]{2,10})[-_ ]?(\d{2,5})(?![0-9])")
FC2_RE = re.compile(r"(?i)FC2[-_ ]?(?:PPV[-_ ]?)?(\d{6,10})")
FALSE_NUMBERS = {"1080", "2160", "720", "480", "360", "1440", "4320"}
FALSE_PREFIXES = {"BHD", "FHD", "UHD", "SHD", "XHD", "XXX"}
SKIP_MARKERS = (
    "萝莉",
    "中学生",
    "小学生",
    "幼女",
    "正太",
    "儿奸",
    "兒童",
    "儿童色情",
    "underage",
    "lolita",
)
VIDEO_EXTS = {".mp4", ".mkv", ".avi", ".wmv", ".ts", ".m2ts", ".iso", ".rmvb", ".webm"}


def extract_code(name: str) -> str | None:
    m = FC2_RE.search(name or "")
    if m:
        return f"FC2-{m.group(1)}"
    for m in CODE_RE.finditer(name or ""):
        prefix, num = m.group(1).upper(), m.group(2)
        if prefix in FALSE_PREFIXES or num in FALSE_NUMBERS:
            continue
        return f"{prefix}-{num}"
    return None


def should_skip(name: str) -> bool:
    low = (name or "").casefold()
    return any(marker.casefold() in low for marker in SKIP_MARKERS)


def openlist_path_from_strm(body: str) -> str | None:
    """Turn ``http://host/d/media/115/...file.mp4`` into ``/media/115/...file.mp4``."""

    line = (body or "").strip().splitlines()[0].strip() if body else ""
    if not line:
        return None
    parsed = urlsplit(line)
    path = unquote(parsed.path or "")
    if path.startswith("/d/"):
        path = path[2:]  # keep leading /
    if not path.startswith("/"):
        path = "/" + path
    # Ignore non-media leftovers.
    if Path(path).suffix.casefold() not in VIDEO_EXTS and not path.lower().endswith(".strm"):
        # still allow known video-less? prefer real videos only
        if Path(path).suffix.casefold() not in VIDEO_EXTS:
            return None
    return path


def load_emby_queue(path: Path) -> dict:
    if path.is_file():
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
    return {"version": 1, "revision": 0, "pending": []}


def enqueue_emby(path: Path, folders: list[str], update_type: str = "Created") -> int:
    if not folders:
        return 0
    state = load_emby_queue(path)
    pending = state.get("pending")
    items: list[dict] = []
    if isinstance(pending, list):
        items = [item for item in pending if isinstance(item, dict)]
    elif isinstance(pending, dict):
        for folder, meta in pending.items():
            if isinstance(meta, dict):
                items.append({"path": folder, **meta})
    listed = {str(item.get("path")) for item in items}
    rev = int(state.get("revision") or 0)
    added = 0
    for folder in folders:
        if folder in listed:
            continue
        rev += 1
        items.append(
            {
                "path": folder,
                "update_type": update_type,
                "revision": rev,
                "attempts": 0,
                "next_attempt": 0.0,
                "last_error": None,
            }
        )
        listed.add(folder)
        added += 1
    state["pending"] = items
    state["revision"] = rev
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    return added



def iter_title_dirs(local_root: Path) -> list[Path]:
    """Folders that directly contain at least one ``.strm`` (OpenList SaveStrm leaves)."""

    found: list[Path] = []
    if not local_root.is_dir():
        return found
    for strm in local_root.rglob("*.strm"):
        if should_skip(str(strm.relative_to(local_root))):
            continue
        parent = strm.parent
        if parent not in found:
            found.append(parent)
    return found


def videos_from_title_dir(directory: Path) -> list[RemoteVideo]:
    videos: list[RemoteVideo] = []
    for strm in sorted(directory.glob("*.strm")):
        try:
            body = strm.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        path = openlist_path_from_strm(body)
        if not path:
            continue
        videos.append(
            RemoteVideo(
                file_id=path,
                name=Path(path).name,
                pick_code=None,
                relative_path=path,
                size=None,
                sign=None,
            )
        )
    return videos


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--local-root",
        default="/media/Cloud/media/115_strm/Pending",
        help="Local OpenList SaveStrm mirror to discover",
    )
    parser.add_argument(
        "--extra-local-root",
        action="append",
        default=[],
        help="Additional local trees (e.g. /media/Cloud/media/115_strm/Porn)",
    )
    parser.add_argument("--move-legacy", action="store_true", help="Move --local-root aside after import")
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    settings.ensure_directories()
    data_dir = Path(settings.data_dir)
    pan = PanService(data_dir=data_dir)
    pan_cfg = pan.config_store.load()
    if not pan_cfg.strm_output_root:
        print("strm_output_root not set", file=sys.stderr)
        return 2
    out_root = Path(pan_cfg.strm_output_root)
    database = Database(settings.database_url)

    roots = [Path(args.local_root), *[Path(p) for p in args.extra_local_root]]
    title_dirs: list[Path] = []
    for root in roots:
        title_dirs.extend(iter_title_dirs(root))
    # de-dupe while preserving order
    seen: set[Path] = set()
    unique: list[Path] = []
    for item in title_dirs:
        key = item.resolve() if item.exists() else item
        if key in seen:
            continue
        seen.add(key)
        unique.append(item)
    title_dirs = unique
    print(
        f"local_roots={[str(r) for r in roots]} out={out_root} "
        f"layout={pan_cfg.strm_layout_template} title_dirs={len(title_dirs)}",
        flush=True,
    )

    stats = {
        "scanned": 0,
        "matched": 0,
        "exported": 0,
        "unchanged": 0,
        "no_code": 0,
        "no_work": 0,
        "no_video": 0,
        "skipped": 0,
        "failed": 0,
    }
    touched: list[str] = []
    orphans: list[str] = []
    samples_before: list[str] = []
    samples_after: list[str] = []
    limit = args.limit or 10**9

    for directory in title_dirs:
        if stats["exported"] + stats["unchanged"] >= limit:
            break
        stats["scanned"] += 1
        name = directory.name
        if should_skip(name) or should_skip(str(directory)):
            stats["skipped"] += 1
            continue
        code = extract_code(name)
        if not code:
            # try parent folder (actor/[CODE] title/file.strm)
            code = extract_code(directory.parent.name) if directory.parent != directory else None
        if not code:
            stats["no_code"] += 1
            orphans.append(str(directory))
            continue
        videos = videos_from_title_dir(directory)
        if not videos:
            stats["no_video"] += 1
            orphans.append(str(directory))
            continue
        with database.session() as session:
            repo = Repository(session)
            work = repo.find_work_by_code(code)
            if work is None:
                stats["no_work"] += 1
                orphans.append(str(directory))
                continue
            identities = repo.identities_for_work(work.id)
            primary = work.primary_code or code
            stats["matched"] += 1
            if len(samples_before) < 8:
                samples_before.append(str(directory))
            if args.dry_run:
                rel = export_relative_dir(primary, work, template=pan_cfg.strm_layout_template)
                print(f"DRY {code} <- {directory} -> {out_root / rel} videos={len(videos)}", flush=True)
                stats["exported"] += 1
                continue
            try:
                result = export_work(
                    settings=pan_cfg,
                    code=primary,
                    videos=videos,
                    work=work,
                    identities=identities,
                )
            except Exception as exc:
                print(f"FAIL export {code}: {exc}", flush=True)
                stats["failed"] += 1
                continue
        if result.changed:
            stats["exported"] += 1
            touched.append(map_to_emby_path(result.directory, pan_cfg))
            if len(samples_after) < 8:
                samples_after.append(str(result.directory))
            print(f"OK {code} -> {result.directory} changed={len(result.changed_paths)}", flush=True)
        else:
            stats["unchanged"] += 1
            if len(samples_after) < 8:
                samples_after.append(str(result.directory))

    queue_path = data_dir / "pan" / "emby-notify-queue.json"
    enqueued = 0 if args.dry_run else enqueue_emby(queue_path, touched, "Created")
    print("STATS", json.dumps(stats, ensure_ascii=False), flush=True)
    print(f"emby_enqueued={enqueued} orphans={len(orphans)}", flush=True)
    print("BEFORE_SAMPLE", json.dumps(samples_before, ensure_ascii=False), flush=True)
    print("AFTER_SAMPLE", json.dumps(samples_after, ensure_ascii=False), flush=True)
    if orphans:
        print("ORPHAN_SAMPLE", json.dumps(orphans[:40], ensure_ascii=False), flush=True)

    if args.move_legacy and not args.dry_run:
        legacy = Path(args.local_root)
        if legacy.is_dir():
            stamp = datetime.now().strftime("%Y%m%dT%H%M%S")
            dest = legacy.parent / f"_legacy_{legacy.name}_{stamp}"
            print(f"Moving {legacy} -> {dest}", flush=True)
            shutil.move(str(legacy), str(dest))
            print(f"legacy={dest}", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
