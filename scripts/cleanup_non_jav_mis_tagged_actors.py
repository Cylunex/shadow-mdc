#!/usr/bin/env python3
"""Remove studio/label/series mis-tags and fuzzy placeholders from non-JAV actors.

Cleans:
  - data/non-jav-actors.json (NonJavActorCatalogStore)
  - data/non-jav-works.json actor arrays (optionally promote studios onto work.studio)
  - SQLite actors / work_actors / works.actors for non-JAV families
  - orphaned actor-images referenced only by removed profiles

Does NOT touch the JAV actor catalog (actor-catalog.json) or JAV works' identity.
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import unicodedata
import uuid
from collections import Counter
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterable

from shadow_mdc.enums import ContentFamily
from shadow_mdc.services.non_jav_actor_catalog import (
    NonJavActorCatalog,
    NonJavActorCatalogStore,
    NonJavActorProfile,
    build_non_jav_actor_profile,
)

# ---------------------------------------------------------------------------
# Curated lists — prefer explicit names over broad regexes.
# NEVER use a naive ``n/?a`` pattern (false-positives: Naimi奶咪, Nana Taipei, Natasha Nice).
# ---------------------------------------------------------------------------

EXPLICIT_STUDIO_LABELS: frozenset[str] = frozenset(
    {
        "91制片厂",
        "OnlyFans华语",
        "乐播传媒",
        "大象传媒",
        "天美传媒",
        "天美女郎",
        "果冻传媒",
        "果冻女孩",
        "精东影业",
        "糖心Vlog",
        "糖心女孩",
        "皇家华人",
        "星空无限传媒",
        "起点传媒",
        "麻豆",
        "麻豆传媒",
        "蜜桃影像",
        "杏吧",
        "海角社区",
        "推特华语博主",
        "推特福利姬",
        "反差博主",
        "丝足福利姬",
        # 91 / 探花 channel brands (not individual performers)
        "91大神",
        "91小严",
        "91小宝",
        "91瓜弟",
        "91探花",
        "夯先生",
        "唐伯虎",
        "弟弟竹竹",
    }
)

EXPLICIT_FUZZY_NAMES: frozenset[str] = frozenset(
    {
        "Amateur",
        "Unknown Guy",
        "Japanese Girl",
        "未知",
        "素人",
        "匿名",
        "不明",
        "无",
        "暂无",
        "N/A",
        "n/a",
        "NA",
        "null",
        "None",
        "演员",
        "女优",
        "女优名",
    }
)

# Names that look studio-ish but are confirmed people — never auto-remove.
KEEP_BORDERLINE: frozenset[str] = frozenset(
    {
        "蜜桃酱",
        "璇璇SWAG",
        "雪碧SWAG",
        "HongKongDoll",
        "完具",
        "国服第一瑶",
        "江南第一深情",  # user-directory handle; treat as a person nickname
        "Naimi奶咪",
        "Nana Taipei",
        "Natasha Nice",
        "软萌白虎",
        "长腿白虎小优",
        "香草少女M",
        "粉色小猪",
        "粉色情人",
        "玉足女王",
        "一条肌肉狗",
        "kaCi脆脆",
    }
)

# Confident alias merges: remove_source -> keep_canonical
CONFIDENT_MERGES: tuple[tuple[str, str], ...] = (
    ("李文雯", "李文文"),  # 李文雯 aliases already include 李文文; zero works
    ("Son Nan Yi", "宋南伊"),  # TPDB latin duplicate of curated 宋南伊
)

_PREFIX_SERIES = (
    "探花",
    "约炮",
    "探店",
    "杏吧",
)
_SUFFIX_SERIES = (
    "探花",
)
_STUDIO_TOKENS = (
    "传媒",
    "制片厂",
    "影业",
    "影像",
    "Vlog",
    "vlog",
)
_FUZZY_EXACT = re.compile(
    r"^(?:未知|素人|匿名|不明|暂无|无|女优|演员|演员\d+|女优\d+|\d+)$",
    re.UNICODE,
)


@dataclass(frozen=True)
class Removal:
    name: str
    reason: str


@dataclass
class CleanupReport:
    before_actors: int = 0
    after_actors: int = 0
    removed: list[dict[str, str]] = field(default_factory=list)
    kept_borderline: list[str] = field(default_factory=list)
    merges: list[dict[str, str]] = field(default_factory=list)
    works_actors_unlinked: int = 0
    works_studio_promoted: int = 0
    sqlite_actors_deleted: int = 0
    sqlite_links_deleted: int = 0
    images_deleted: int = 0
    collections_seeded: dict[str, int] = field(default_factory=dict)
    leftover_suspects: list[str] = field(default_factory=list)
    by_reason: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object]:
        return {
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "before_actors": self.before_actors,
            "after_actors": self.after_actors,
            "removed_count": len(self.removed),
            "removed_by_reason": dict(sorted(self.by_reason.items())),
            "sample_removed": [item["name"] for item in self.removed[:40]],
            "removed": self.removed,
            "kept_borderline": self.kept_borderline,
            "merges": self.merges,
            "works_actors_unlinked": self.works_actors_unlinked,
            "works_studio_promoted": self.works_studio_promoted,
            "sqlite_actors_deleted": self.sqlite_actors_deleted,
            "sqlite_links_deleted": self.sqlite_links_deleted,
            "images_deleted": self.images_deleted,
            "collections_seeded": self.collections_seeded,
            "leftover_suspects": self.leftover_suspects,
        }


def normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


def classify_removal(name: str) -> str | None:
    """Return removal reason or None to keep."""

    cleaned = name.strip()
    if not cleaned:
        return "fuzzy"
    if cleaned in KEEP_BORDERLINE:
        return None
    if cleaned in EXPLICIT_FUZZY_NAMES or _FUZZY_EXACT.fullmatch(cleaned):
        return "fuzzy"
    if cleaned in EXPLICIT_STUDIO_LABELS:
        return "studio_label"
    # Prefix / suffix series brands
    for prefix in _PREFIX_SERIES:
        if cleaned.startswith(prefix) and cleaned != prefix:
            # 探花* / 约炮* / 探店* / 杏吧* channel labels
            return "studio_label"
    for suffix in _SUFFIX_SERIES:
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            return "studio_label"
    for token in _STUDIO_TOKENS:
        if token in cleaned and cleaned not in KEEP_BORDERLINE:
            # Avoid wiping real people whose nickname merely contains a token —
            # require the name to look like a brand (short, or ends with token, or exact known).
            if cleaned.endswith(token) or cleaned.endswith("传媒") or cleaned.endswith("影业"):
                return "studio_label"
    return None


def build_removals(actors: Iterable[NonJavActorProfile | dict[str, object]]) -> list[Removal]:
    removals: list[Removal] = []
    for actor in actors:
        name = actor.name if isinstance(actor, NonJavActorProfile) else str(actor.get("name") or "")
        reason = classify_removal(name)
        if reason:
            removals.append(Removal(name=name, reason=reason))
    return removals


def _load_works_payload(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def _save_json(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


def _merge_aliases_into(
    catalog: NonJavActorCatalog,
    *,
    source_name: str,
    target_name: str,
) -> NonJavActorCatalog:
    by_key = {normalize(actor.name): actor for actor in catalog.actors}
    source = by_key.get(normalize(source_name))
    target = by_key.get(normalize(target_name))
    if source is None or target is None:
        return catalog
    aliases = tuple(
        dict.fromkeys(
            (
                *target.aliases,
                source.name,
                *source.aliases,
            )
        )
    )
    groups = tuple(dict.fromkeys((*target.groups, *source.groups)))
    categories = tuple(dict.fromkeys((*target.categories, *source.categories)))
    merged = build_non_jav_actor_profile(
        name=target.name,
        aliases=aliases,
        groups=groups,
        categories=categories,
        image_file=target.image_file or source.image_file,
        x_handle=target.x_handle or source.x_handle,
        biography=target.biography or source.biography,
        notes=target.notes or source.notes,
    )
    retained = [
        actor
        for actor in catalog.actors
        if normalize(actor.name) not in {normalize(source_name), normalize(target_name)}
    ]
    retained.append(merged)
    retained.sort(key=lambda actor: actor.name.casefold())
    return catalog.model_copy(update={"actors": tuple(retained)})


def _clean_works_json(
    payload: dict[str, object],
    *,
    removed_keys: set[str],
    studio_keys: set[str],
    merge_map: dict[str, str],
) -> tuple[dict[str, object], int, int]:
    works = payload.get("works")
    if not isinstance(works, list):
        return payload, 0, 0
    unlinked = 0
    promoted = 0
    for item in works:
        if not isinstance(item, dict):
            continue
        actors = item.get("actors")
        if not isinstance(actors, list):
            continue
        new_actors: list[str] = []
        removed_studios: list[str] = []
        for raw in actors:
            name = str(raw).strip()
            if not name:
                continue
            key = normalize(name)
            if key in merge_map:
                name = merge_map[key]
                key = normalize(name)
            if key in removed_keys:
                unlinked += 1
                if key in studio_keys:
                    removed_studios.append(name)
                continue
            new_actors.append(name)
        # de-dupe while preserving order
        item["actors"] = list(dict.fromkeys(new_actors))
        studio = item.get("studio")
        if (not studio or not str(studio).strip()) and removed_studios:
            item["studio"] = removed_studios[0]
            promoted += 1
    return payload, unlinked, promoted


def _clean_sqlite(
    database: Path,
    *,
    removed_keys: set[str],
    studio_keys: set[str],
    merge_map: dict[str, str],
    dry_run: bool,
) -> tuple[int, int, int]:
    """Update non-JAV works.actors, drop links, delete orphan removed actors.

    Returns (works_touched_actor_edits, links_deleted, actors_deleted).
    """

    if not database.is_file():
        return 0, 0, 0

    conn = sqlite3.connect(database)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA foreign_keys = ON")
        works = conn.execute(
            "SELECT id, family, category, actors, studio FROM works WHERE family != ?",
            (ContentFamily.JAV.value,),
        ).fetchall()
        works_edited = 0
        for row in works:
            try:
                actors = json.loads(row["actors"] or "[]")
            except json.JSONDecodeError:
                actors = []
            if not isinstance(actors, list):
                continue
            new_actors: list[str] = []
            changed = False
            promoted_studio: str | None = None
            for raw in actors:
                name = str(raw).strip()
                if not name:
                    changed = True
                    continue
                key = normalize(name)
                if key in merge_map:
                    name = merge_map[key]
                    key = normalize(name)
                    changed = True
                if key in removed_keys:
                    changed = True
                    if key in studio_keys and not promoted_studio:
                        promoted_studio = name
                    continue
                new_actors.append(name)
            new_actors = list(dict.fromkeys(new_actors))
            studio = row["studio"]
            new_studio = studio
            if promoted_studio and (not studio or not str(studio).strip()):
                new_studio = promoted_studio
                changed = True
            if not changed and new_actors == list(actors):
                continue
            works_edited += 1
            if dry_run:
                continue
            conn.execute(
                "UPDATE works SET actors = ?, studio = ?, updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (json.dumps(new_actors, ensure_ascii=False), new_studio, row["id"]),
            )
            # rebuild work_actors for this work
            conn.execute("DELETE FROM work_actors WHERE work_id = ?", (row["id"],))
            for position, name in enumerate(new_actors):
                normalized = normalize(name)
                actor_row = conn.execute(
                    "SELECT id FROM actors WHERE normalized_name = ?",
                    (normalized,),
                ).fetchone()
                if actor_row is None:
                    actor_id = str(uuid.uuid4())
                    conn.execute(
                        "INSERT INTO actors (id, name, normalized_name, aliases, created_at, updated_at) "
                        "VALUES (?, ?, ?, '[]', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
                        (actor_id, name, normalized),
                    )
                else:
                    actor_id = actor_row["id"]
                conn.execute(
                    "INSERT INTO work_actors (work_id, actor_id, position) VALUES (?, ?, ?)",
                    (row["id"], actor_id, position),
                )

        # Delete actors whose normalized_name is in removed_keys and who are not
        # linked to any remaining work (including JAV — safety).
        placeholders = ",".join("?" for _ in removed_keys) or "NULL"
        # Also delete merge sources
        all_delete_keys = set(removed_keys) | set(merge_map.keys())
        placeholders = ",".join("?" for _ in all_delete_keys) or "NULL"
        params = tuple(all_delete_keys)

        link_rows = conn.execute(
            f"""
            SELECT wa.actor_id, wa.work_id
            FROM work_actors wa
            JOIN actors a ON a.id = wa.actor_id
            WHERE a.normalized_name IN ({placeholders})
            """,
            params,
        ).fetchall() if all_delete_keys else []

        links_deleted = 0
        for link in link_rows:
            # Only drop link if the work is non-JAV
            family = conn.execute(
                "SELECT family FROM works WHERE id = ?",
                (link["work_id"],),
            ).fetchone()
            if family and family["family"] == ContentFamily.JAV.value:
                continue
            links_deleted += 1
            if not dry_run:
                conn.execute(
                    "DELETE FROM work_actors WHERE work_id = ? AND actor_id = ?",
                    (link["work_id"], link["actor_id"]),
                )

        actor_rows = conn.execute(
            f"SELECT id, normalized_name FROM actors WHERE normalized_name IN ({placeholders})",
            params,
        ).fetchall() if all_delete_keys else []
        actors_deleted = 0
        for actor in actor_rows:
            still_linked = conn.execute(
                "SELECT 1 FROM work_actors WHERE actor_id = ? LIMIT 1",
                (actor["id"],),
            ).fetchone()
            if still_linked:
                continue
            actors_deleted += 1
            if not dry_run:
                conn.execute("DELETE FROM actors WHERE id = ?", (actor["id"],))

        if not dry_run:
            conn.commit()
        return works_edited, links_deleted, actors_deleted
    finally:
        conn.close()


def _delete_orphan_images(
    *,
    images_dir: Path,
    removed_profiles: list[NonJavActorProfile],
    remaining: NonJavActorCatalog,
    dry_run: bool,
) -> int:
    kept_files = {
        actor.image_file
        for actor in remaining.actors
        if actor.image_file
    }
    deleted = 0
    for profile in removed_profiles:
        image = profile.image_file
        if not image or image in kept_files:
            continue
        path = images_dir / image
        if not path.is_file():
            continue
        deleted += 1
        if not dry_run:
            path.unlink(missing_ok=True)
    return deleted


def _scan_leftover_suspects(actors: Iterable[NonJavActorProfile]) -> list[str]:
    suspects: list[str] = []
    for actor in actors:
        name = actor.name
        if name in KEEP_BORDERLINE:
            continue
        bio = (actor.biography or "").lower()
        if any(
            marker in (actor.biography or "")
            for marker in ("系列名", "系列创作者", "聚合名", "placeholder actor", "平台占位")
        ):
            suspects.append(name)
            continue
        if any(token in name for token in ("探花", "约炮", "探店", "杏吧", "传媒", "制片")):
            suspects.append(name)
            continue
        if "studio" in bio or "平台" in (actor.biography or ""):
            if any(g in actor.groups for g in ("madou", "tanhua", "91-tanhua", "91-studio")):
                # already kept people may have these groups; only flag if name looks brand-like
                if name.endswith(("厂", "传媒", "影业", "社区")):
                    suspects.append(name)
    return sorted(set(suspects))


def run_cleanup(
    *,
    data_dir: Path,
    database: Path,
    report_path: Path,
    dry_run: bool,
    seed_collections: bool,
) -> CleanupReport:
    actor_path = data_dir / "non-jav-actors.json"
    works_path = data_dir / "non-jav-works.json"
    images_dir = data_dir / "actor-images"
    store = NonJavActorCatalogStore(actor_path)
    catalog = store.load()

    report = CleanupReport(before_actors=len(catalog.actors))
    report.kept_borderline = sorted(
        actor.name for actor in catalog.actors if actor.name in KEEP_BORDERLINE
    )

    # Merges first (so merged-away names are gone before removal classification printout)
    merge_map: dict[str, str] = {}
    for source, target in CONFIDENT_MERGES:
        if store.get(source) and store.get(target):
            catalog = _merge_aliases_into(catalog, source_name=source, target_name=target)
            merge_map[normalize(source)] = target
            report.merges.append({"from": source, "into": target, "reason": "confident_alias_dupe"})

    removals = build_removals(catalog.actors)
    removed_keys = {normalize(item.name) for item in removals}
    studio_keys = {normalize(item.name) for item in removals if item.reason == "studio_label"}
    # also treat explicit studio list as studio for promotion even if classified fuzzy (none today)
    studio_keys |= {normalize(name) for name in EXPLICIT_STUDIO_LABELS}

    report.by_reason = dict(Counter(item.reason for item in removals))
    report.removed = [{"name": item.name, "reason": item.reason} for item in removals]

    print(f"before actors: {report.before_actors}")
    print(f"planned removals: {len(removals)}  by_reason={report.by_reason}")
    print(f"planned merges: {len(report.merges)}")
    for reason, count in sorted(report.by_reason.items()):
        sample = [item.name for item in removals if item.reason == reason][:12]
        print(f"  {reason}: {count}  sample={sample}")

    removed_profiles = [
        actor for actor in catalog.actors if normalize(actor.name) in removed_keys
    ]
    retained = tuple(
        actor for actor in catalog.actors if normalize(actor.name) not in removed_keys
    )
    retained_catalog = catalog.model_copy(update={"actors": retained})

    # works JSON
    works_payload = _load_works_payload(works_path) if works_path.is_file() else {"version": 1, "works": []}
    works_payload, unlinked, promoted = _clean_works_json(
        works_payload,
        removed_keys=removed_keys,
        studio_keys=studio_keys,
        merge_map=merge_map,
    )
    report.works_actors_unlinked = unlinked
    report.works_studio_promoted = promoted

    works_edited, links_deleted, actors_deleted = _clean_sqlite(
        database,
        removed_keys=removed_keys,
        studio_keys=studio_keys,
        merge_map=merge_map,
        dry_run=dry_run,
    )
    report.sqlite_links_deleted = links_deleted
    report.sqlite_actors_deleted = actors_deleted
    print(
        f"sqlite non-jav works edited={works_edited} "
        f"links_deleted={links_deleted} actors_deleted={actors_deleted}"
    )

    images_deleted = _delete_orphan_images(
        images_dir=images_dir,
        removed_profiles=removed_profiles,
        remaining=retained_catalog,
        dry_run=dry_run,
    )
    report.images_deleted = images_deleted

    if not dry_run:
        store.save(retained_catalog)
        if works_path.is_file():
            _save_json(works_path, works_payload)

        if seed_collections and database.is_file():
            from shadow_mdc.db.repository import Database, Repository

            # Prefer explicit database URL so NAS/shared paths work.
            db = Database(f"sqlite:///{database.resolve()}")
            db.initialize()
            with db.session() as session:
                repo = Repository(session)
                report.collections_seeded = repo.seed_collections_from_works()
                session.commit()

    final_catalog = store.load() if not dry_run else retained_catalog
    report.after_actors = len(final_catalog.actors)
    report.leftover_suspects = _scan_leftover_suspects(final_catalog.actors)

    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(
        json.dumps(report.to_dict(), ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print(f"after actors: {report.after_actors}")
    print(f"report → {report_path}")
    if report.leftover_suspects:
        print(f"leftover suspects ({len(report.leftover_suspects)}): {report.leftover_suspects[:20]}")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=Path("data"))
    parser.add_argument("--database", type=Path, default=None)
    parser.add_argument(
        "--report",
        type=Path,
        default=Path("/workspace/exports/non-jav-cleanup-20260910.json"),
    )
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--seed-collections",
        action="store_true",
        default=True,
        help="after cleanup, seed Collection rows from work studio/series/label (default on)",
    )
    parser.add_argument(
        "--no-seed-collections",
        action="store_false",
        dest="seed_collections",
    )
    args = parser.parse_args()
    database = args.database or (args.data_dir / "shadow-mdc.db")
    run_cleanup(
        data_dir=args.data_dir,
        database=database,
        report_path=args.report,
        dry_run=args.dry_run,
        seed_collections=args.seed_collections,
    )


if __name__ == "__main__":
    main()
