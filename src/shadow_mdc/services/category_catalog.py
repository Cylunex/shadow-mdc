"""Curated category covers catalog (Pornhub-style grid).

Manifest lives under ``data/category-covers/manifest.json`` (gitignored with
``data/``). Seed script downloads covers; API serves metadata + facet counts.
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from shadow_mdc.tags import canonicalize_tag, normalize_tags

MANIFEST_NAME = "manifest.json"
COVERS_DIRNAME = "category-covers"

# Curated featured labels (~40-80). Prefer COMMON_FILTER_TAGS + popular PH/XH genres.
FEATURED_CATEGORY_LABELS: tuple[str, ...] = (
    "素人",
    "巨乳",
    "美尻",
    "贫乳",
    "口交",
    "深喉",
    "中出",
    "颜射",
    "射精",
    "潮吹",
    "自慰",
    "手交",
    "足交",
    "肛交",
    "双龙",
    "POV",
    "MILF",
    "熟女",
    "人妻",
    "美少女",
    "学生",
    "OL",
    "护士",
    "女仆",
    "老师",
    "继母",
    "女同",
    "多人",
    "Cosplay",
    "角色扮演",
    "制服",
    "黑丝",
    "丝袜",
    "网袜",
    "高跟鞋",
    "内衣",
    "眼镜",
    "美腿",
    "痴女",
    "娇小",
    "苗条",
    "亚洲",
    "日本",
    "韩国",
    "BBC",
    "BBW",
    "BDSM",
    "捆绑",
    "玩具",
    "按摩",
    "露出",
    "户外",
    "NTR",
    "近亲",
    "里番",
    "动漫",
    "VR",
    "4K",
    "单体作品",
    "Interracial",
    "Latina",
    "Ebony",
    "Blonde",
    "Brunette",
    "Redhead",
    "Shemale",
    "合集",
    "试镜",
    "酒店",
    "孕妇",
    "纹身",
    "癖好",
    "高潮",
    "接吻",
)


def covers_dir(data_dir: Path) -> Path:
    return Path(data_dir) / COVERS_DIRNAME


def manifest_path(data_dir: Path) -> Path:
    return covers_dir(data_dir) / MANIFEST_NAME


def slugify_label(label: str) -> str:
    """Stable URL-ish slug for a canonical label (ASCII + CN preserved)."""

    text = unicodedata.normalize("NFKC", label).strip()
    text = text.replace(" ", "-")
    text = re.sub(r"[^\w\-一-龥ぁ-んァ-ン]+", "-", text, flags=re.UNICODE)
    text = re.sub(r"-{2,}", "-", text).strip("-")
    return text.casefold() or "category"


@dataclass(frozen=True)
class CategoryCoverEntry:
    slug: str
    label: str
    image_file: str | None
    aliases: tuple[str, ...] = ()
    source: str | None = None
    source_title: str | None = None

    @property
    def image_url(self) -> str | None:
        if not self.image_file:
            return None
        return f"/api/category-covers/{self.image_file}"


@dataclass(frozen=True)
class CategoryOutData:
    slug: str
    label: str
    image_url: str | None
    work_count: int
    aliases: tuple[str, ...]


def load_manifest(data_dir: Path) -> list[CategoryCoverEntry]:
    path = manifest_path(data_dir)
    if not path.is_file():
        return []
    raw = json.loads(path.read_text(encoding="utf-8"))
    items = raw.get("categories") if isinstance(raw, dict) else raw
    if not isinstance(items, list):
        return []
    entries: list[CategoryCoverEntry] = []
    seen: set[str] = set()
    for item in items:
        if not isinstance(item, dict):
            continue
        label = canonicalize_tag(str(item.get("label") or "")) or str(item.get("label") or "").strip()
        if not label:
            continue
        slug = str(item.get("slug") or slugify_label(label))
        if slug in seen:
            continue
        seen.add(slug)
        aliases_raw = item.get("aliases") or []
        aliases = tuple(
            str(a).strip()
            for a in aliases_raw
            if isinstance(a, str) and str(a).strip() and str(a).strip() != label
        )
        image_file = item.get("image_file") or item.get("image")
        if image_file is not None:
            image_file = Path(str(image_file)).name
            if not image_file:
                image_file = None
        entries.append(
            CategoryCoverEntry(
                slug=slug,
                label=label,
                image_file=image_file,
                aliases=aliases,
                source=str(item["source"]) if item.get("source") else None,
                source_title=str(item["source_title"]) if item.get("source_title") else None,
            )
        )
    return entries


def write_manifest(data_dir: Path, entries: list[CategoryCoverEntry]) -> Path:
    directory = covers_dir(data_dir)
    directory.mkdir(parents=True, exist_ok=True)
    path = manifest_path(data_dir)
    payload: dict[str, Any] = {
        "version": 1,
        "categories": [
            {
                "slug": entry.slug,
                "label": entry.label,
                "image_file": entry.image_file,
                "aliases": list(entry.aliases),
                "source": entry.source,
                "source_title": entry.source_title,
            }
            for entry in entries
        ],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return path


def build_category_list(
    data_dir: Path,
    *,
    facet_counts: dict[str, int] | None = None,
    only_with_works: bool = False,
) -> list[CategoryOutData]:
    """Merge manifest entries with library facet counts.

    When no manifest exists, fall back to FEATURED_CATEGORY_LABELS with null images.
    """

    counts = facet_counts or {}
    entries = load_manifest(data_dir)
    if not entries:
        entries = [
            CategoryCoverEntry(slug=slugify_label(label), label=label, image_file=None)
            for label in FEATURED_CATEGORY_LABELS
        ]

    # Prefer featured order; append any extra manifest entries.
    order = {label: index for index, label in enumerate(FEATURED_CATEGORY_LABELS)}
    by_label = {entry.label: entry for entry in entries}
    ordered_labels: list[str] = []
    for label in FEATURED_CATEGORY_LABELS:
        if label in by_label:
            ordered_labels.append(label)
    for entry in entries:
        if entry.label not in ordered_labels:
            ordered_labels.append(entry.label)

    results: list[CategoryOutData] = []
    for label in ordered_labels:
        entry = by_label[label]
        count = int(counts.get(label, 0))
        if only_with_works and count <= 0:
            continue
        results.append(
            CategoryOutData(
                slug=entry.slug,
                label=entry.label,
                image_url=entry.image_url,
                work_count=count,
                aliases=entry.aliases,
            )
        )
    # Stable secondary sort within equal featured rank by count desc
    results.sort(
        key=lambda item: (
            order.get(item.label, 10_000),
            -item.work_count,
            item.label.casefold(),
        )
    )
    return results


def facet_count_map(works_tags: list[list[str] | tuple[str, ...] | None]) -> dict[str, int]:
    counter: dict[str, int] = {}
    for tags in works_tags:
        for name in normalize_tags(tags):
            counter[name] = counter.get(name, 0) + 1
    return counter


def resolve_cover_file(data_dir: Path, filename: str) -> Path | None:
    """Return absolute path if ``filename`` is a safe cover referenced by the manifest."""

    if Path(filename).name != filename:
        return None
    if not filename or filename.startswith("."):
        return None
    path = covers_dir(data_dir) / filename
    if not path.is_file():
        return None
    referenced = {
        entry.image_file
        for entry in load_manifest(data_dir)
        if entry.image_file
    }
    # When a manifest exists with covers, only serve referenced files.
    if referenced and filename not in referenced:
        return None
    return path
