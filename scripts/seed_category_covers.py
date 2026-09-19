#!/usr/bin/env python3
"""Seed curated category cover images into data/category-covers/.

Maps Pornhub (CN) / xHamster / Eporner category titles onto our tags.py
canonical labels, downloads one cover per featured category (prefer PH → XH → EP),
and writes manifest.json. Idempotent: skips existing image files.

Example::

    PYTHONPATH=src .venv/bin/python scripts/seed_category_covers.py
    PYTHONPATH=src .venv/bin/python scripts/seed_category_covers.py --dry-run
    PYTHONPATH=src .venv/bin/python scripts/seed_category_covers.py \\
        --refs /workspace/category-refs/parsed.json
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.services.category_catalog import (  # noqa: E402
    FEATURED_CATEGORY_LABELS,
    CategoryCoverEntry,
    covers_dir,
    load_manifest,
    slugify_label,
    write_manifest,
)
from shadow_mdc.tags import (  # noqa: E402
    _CANONICAL_SYNONYMS,
    canonicalize_tag,
    synonyms_for,
)

_IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".webp", ".gif"}

_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/128.0.0.0 Safari/537.36"
)

# Prefer these display labels when multiple PH titles collapse to the same tag.
_LABEL_PRIORITY: dict[str, int] = {
    label: index for index, label in enumerate(FEATURED_CATEGORY_LABELS)
}


def _ext_from_url_or_content(url: str, content: bytes) -> str:
    path = url.split("?", 1)[0]
    guessed = Path(path).suffix.lower()
    if guessed in _IMAGE_EXTS:
        return ".jpg" if guessed == ".jpeg" else guessed
    if content[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if content[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    if content[:4] == b"RIFF" and content[8:12] == b"WEBP":
        return ".webp"
    if content[:6] in {b"GIF87a", b"GIF89a"}:
        return ".gif"
    return ".jpg"


def _download(url: str, dest: Path, *, timeout: float = 30.0) -> bool:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": _BROWSER_UA,
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
            "Referer": "https://www.pornhub.com/",
        },
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            content = response.read()
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        print(f"  download failed: {url} ({exc})", file=sys.stderr)
        return False
    if not content or len(content) < 200:
        print(f"  download too small: {url}", file=sys.stderr)
        return False
    ext = _ext_from_url_or_content(url, content)
    final = dest if dest.suffix else dest.with_suffix(ext)
    temporary = final.with_suffix(final.suffix + ".tmp")
    temporary.write_bytes(content)
    temporary.replace(final)
    return True


def _load_refs(path: Path) -> dict[str, list[dict]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    sources = data.get("sources") if isinstance(data, dict) else None
    if not isinstance(sources, dict):
        raise SystemExit(f"invalid refs file: {path}")
    result: dict[str, list[dict]] = {}
    for key in ("pornhub", "xhamster", "eporner"):
        items = sources.get(key) or []
        if not isinstance(items, list):
            items = []
        result[key] = [item for item in items if isinstance(item, dict)]
    return result


def _pick_image(item: dict) -> str | None:
    for key in ("image", "thumb", "cover", "img"):
        value = item.get(key)
        if isinstance(value, str) and value.startswith("http"):
            return value
    return None


def _build_source_index(
    refs: dict[str, list[dict]],
) -> dict[str, list[tuple[str, str, str]]]:
    """canonical label → list of (priority_source, source_title, image_url).

    Source priority order when consuming: pornhub, xhamster, eporner.
    """

    source_rank = {"pornhub": 0, "xhamster": 1, "eporner": 2}
    index: dict[str, list[tuple[str, str, str]]] = {}
    for source_name, items in refs.items():
        for item in items:
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            image = _pick_image(item)
            if not image:
                continue
            canonical = canonicalize_tag(title)
            if canonical is None:
                continue
            # Only keep featured + known taxonomy
            if (
                canonical not in FEATURED_CATEGORY_LABELS
                and canonical not in _CANONICAL_SYNONYMS
            ):
                continue
            index.setdefault(canonical, []).append((source_name, title, image))
    # Sort each label's candidates: PH first, then XH, then EP; stable
    for _label, candidates in index.items():
        candidates.sort(key=lambda row: (source_rank.get(row[0], 9), row[1]))
    return index


def _aliases_for(label: str) -> tuple[str, ...]:
    values: list[str] = []
    seen: set[str] = set()
    for item in synonyms_for(label):
        text = str(item).strip()
        if not text or text == label:
            continue
        key = text.casefold()
        if key in seen:
            continue
        seen.add(key)
        values.append(text)
    return tuple(values[:24])


def seed(
    *,
    data_dir: Path,
    refs_path: Path,
    dry_run: bool = False,
    force: bool = False,
    labels: tuple[str, ...] | None = None,
) -> int:
    refs = _load_refs(refs_path)
    index = _build_source_index(refs)
    target_labels = list(labels) if labels else list(FEATURED_CATEGORY_LABELS)
    # Also include any featured-mapped labels discovered only via sources
    for label in sorted(index.keys(), key=lambda name: _LABEL_PRIORITY.get(name, 10_000)):
        if label not in target_labels and label in FEATURED_CATEGORY_LABELS:
            target_labels.append(label)

    out_dir = covers_dir(data_dir)
    if not dry_run:
        out_dir.mkdir(parents=True, exist_ok=True)

    existing_by_label = {entry.label: entry for entry in load_manifest(data_dir)}
    entries: list[CategoryCoverEntry] = []
    downloaded = 0
    skipped = 0
    missing_cover = 0
    updated_labels: set[str] = set()

    for label in target_labels:
        if label not in FEATURED_CATEGORY_LABELS and label not in index:
            continue
        slug = slugify_label(label)
        candidates = index.get(label) or []
        image_file: str | None = None
        source: str | None = None
        source_title: str | None = None

        if candidates:
            source, source_title, url = candidates[0]
            # Deterministic filename from slug + source
            digest = hashlib.sha1(f"{slug}:{source}:{url}".encode()).hexdigest()[:10]
            # Probe extension later; start with .jpg placeholder name pattern
            candidate_names = list(out_dir.glob(f"{slug}-{digest}.*")) if out_dir.exists() else []
            existing = next(
                (path for path in candidate_names if path.suffix.lower() in _IMAGE_EXTS),
                None,
            )
            if existing and not force:
                image_file = existing.name
                skipped += 1
            elif dry_run:
                image_file = f"{slug}-{digest}.jpg"
                print(f"[dry-run] {label} ← {source}/{source_title} {url[:80]}")
            else:
                dest = out_dir / f"{slug}-{digest}"
                # Remove prior variants when forcing
                if force:
                    for path in out_dir.glob(f"{slug}-{digest}.*"):
                        if path.suffix.endswith(".tmp"):
                            continue
                        path.unlink(missing_ok=True)
                ok = _download(url, dest)
                if ok:
                    # Find written file
                    written = next(
                        (
                            path
                            for path in out_dir.glob(f"{slug}-{digest}.*")
                            if path.suffix.lower() in _IMAGE_EXTS
                        ),
                        None,
                    )
                    if written is None:
                        # _download may have used dest with suffix
                        for ext in (".jpg", ".png", ".webp", ".gif"):
                            trial = dest.with_suffix(ext)
                            if trial.is_file():
                                written = trial
                                break
                    if written is not None:
                        image_file = written.name
                        downloaded += 1
                        print(f"OK {label} ← {source}/{source_title} → {image_file}")
                    else:
                        missing_cover += 1
                        print(f"MISS write {label}", file=sys.stderr)
                else:
                    # Try remaining candidates
                    saved = False
                    for alt_source, alt_title, alt_url in candidates[1:]:
                        alt_digest = hashlib.sha1(
                            f"{slug}:{alt_source}:{alt_url}".encode()
                        ).hexdigest()[:10]
                        alt_dest = out_dir / f"{slug}-{alt_digest}"
                        if _download(alt_url, alt_dest):
                            written = next(
                                (
                                    path
                                    for path in out_dir.glob(f"{slug}-{alt_digest}.*")
                                    if path.suffix.lower() in _IMAGE_EXTS
                                ),
                                None,
                            )
                            if written is not None:
                                image_file = written.name
                                source, source_title = alt_source, alt_title
                                downloaded += 1
                                saved = True
                                print(
                                    f"OK {label} ← {alt_source}/{alt_title} → {image_file}"
                                )
                                break
                    if not saved:
                        missing_cover += 1
                        print(f"MISS {label} (all sources failed)", file=sys.stderr)
        else:
            # Keep a previously seeded cover when refs have no matching title.
            prior = existing_by_label.get(label)
            if prior and prior.image_file and (out_dir / prior.image_file).is_file():
                image_file = prior.image_file
                source = prior.source
                source_title = prior.source_title
                skipped += 1
                print(f"KEEP {label} ← existing {image_file}")
            else:
                missing_cover += 1
                print(f"NO-SRC {label}", file=sys.stderr)

        entries.append(
            CategoryCoverEntry(
                slug=slug,
                label=label,
                image_file=image_file,
                aliases=_aliases_for(label),
                source=source,
                source_title=source_title,
            )
        )
        updated_labels.add(label)

    # When seeding a subset, keep other curated entries from the existing manifest.
    if labels is not None:
        for label, entry in existing_by_label.items():
            if label not in updated_labels:
                entries.append(entry)
        entries.sort(
            key=lambda item: (
                _LABEL_PRIORITY.get(item.label, 10_000),
                item.label.casefold(),
            )
        )

    if dry_run:
        print(
            f"dry-run: would write {len(entries)} categories "
            f"(download≈{sum(1 for e in entries if e.image_file)}, missing={missing_cover})"
        )
        return 0

    path = write_manifest(data_dir, entries)
    with_images = sum(1 for entry in entries if entry.image_file)
    print(
        f"manifest → {path}  categories={len(entries)} "
        f"covers={with_images} downloaded={downloaded} skipped={skipped} missing={missing_cover}"
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--data-dir",
        type=Path,
        default=None,
        help="Override SHADOW_MDC data dir (default from settings)",
    )
    parser.add_argument(
        "--refs",
        type=Path,
        default=Path("/workspace/category-refs/parsed.json"),
        help="Parsed category reference JSON",
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true", help="Re-download even if file exists")
    parser.add_argument(
        "--label",
        action="append",
        default=[],
        help="Limit to specific canonical label(s); repeatable",
    )
    args = parser.parse_args(argv)
    updates: dict[str, object] = {}
    if args.data_dir is not None:
        updates["data_dir"] = args.data_dir
    settings = Settings(**updates) if updates else Settings()
    settings.ensure_directories()
    (settings.data_dir / "category-covers").mkdir(parents=True, exist_ok=True)
    if not args.refs.is_file():
        print(f"refs not found: {args.refs}", file=sys.stderr)
        return 2
    labels = tuple(args.label) if args.label else None
    return seed(
        data_dir=settings.data_dir,
        refs_path=args.refs,
        dry_run=args.dry_run,
        force=args.force,
        labels=labels,
    )


if __name__ == "__main__":
    raise SystemExit(main())
