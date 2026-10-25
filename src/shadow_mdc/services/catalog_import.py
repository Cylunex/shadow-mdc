"""Merge-import portable catalog bundles without wiping runtime state."""

from __future__ import annotations

import json
import shutil
import sqlite3
import tarfile
import tempfile
import unicodedata
import zipfile
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from pydantic import BaseModel, ConfigDict, ValidationError

from ..db.models import Work
from ..db.repository import Repository
from ..domain import ProviderRecord
from ..identity import IdentityAliasRules
from .actor_catalog import ActorCatalogStore, merge_actor_catalogs
from .alias_store import IdentityAliasStore, default_alias_rules
from .non_jav_actor_catalog import (
    NonJavActorCatalog,
    NonJavActorCatalogStore,
    NonJavActorProfile,
    build_non_jav_actor_profile,
)
from .non_jav_work_seed import seed_non_jav_works
from .path_filter import FilterWords, FilterWordsStore

_ACTOR_IMAGE_NAMES = ("non-jav-actors.json",)


class CatalogImportRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dry_run: bool = False
    actors_only: bool = False
    works_only: bool = False
    include_formal: bool = True


class CatalogImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    dry_run: bool
    bundle_kind: str
    actors_added: int = 0
    actors_updated: int = 0
    actors_unchanged: int = 0
    actor_images_copied: int = 0
    works_created: int = 0
    works_updated: int = 0
    works_posters: int = 0
    works_actors_added: int = 0
    artwork_copied: int = 0
    formal_works_imported: int = 0
    jav_actors_merged: int = 0
    aliases_keys_added: int = 0
    filter_words_added: int = 0
    notes: tuple[str, ...] = ()


@dataclass(frozen=True)
class _ResolvedBundle:
    root: Path
    data_dir: Path
    kind: str


def import_catalog_bundle(
    *,
    bundle: Path,
    data_dir: Path,
    repo: Repository | None,
    actor_store: NonJavActorCatalogStore,
    actor_images_dir: Path,
    artwork_dir: Path,
    request: CatalogImportRequest | None = None,
    actor_catalog_store: ActorCatalogStore | None = None,
    alias_store: IdentityAliasStore | None = None,
    filter_words_store: FilterWordsStore | None = None,
) -> CatalogImportResult:
    """Merge actors/works/media from a portable bundle into the local data directory."""

    options = request or CatalogImportRequest()
    if options.actors_only and options.works_only:
        raise ValueError("cannot combine --actors-only with --works-only")

    data_dir = data_dir.resolve()
    actor_images_dir = actor_images_dir.resolve()
    artwork_dir = artwork_dir.resolve()
    data_dir.mkdir(parents=True, exist_ok=True)
    actor_images_dir.mkdir(parents=True, exist_ok=True)
    artwork_dir.mkdir(parents=True, exist_ok=True)

    notes: list[str] = []
    with _open_bundle(bundle) as resolved:
        actors_added = actors_updated = actors_unchanged = images_copied = 0
        works_created = works_updated = works_posters = works_actors_added = 0
        artwork_copied = formal_imported = jav_merged = aliases_added = filters_added = 0

        if not options.works_only:
            source_actors_path = _find_file(resolved.data_dir, *_ACTOR_IMAGE_NAMES)
            actor_stats = _merge_non_jav_actors(
                actor_store=actor_store,
                source_catalog_path=source_actors_path,
                source_images_dir=resolved.data_dir / "actor-images",
                target_images_dir=actor_images_dir,
                dry_run=options.dry_run,
            )
            actors_added, actors_updated, actors_unchanged, images_copied = actor_stats
            if source_actors_path is None:
                notes.append("no non-jav-actors.json found in bundle")
            # Split chat packs ship actor-images without JSON; merge the tree like artwork.
            images_copied += _copy_missing_tree(
                resolved.data_dir / "actor-images",
                actor_images_dir,
                dry_run=options.dry_run,
            )

            if options.include_formal:
                jav_merged = _merge_jav_actor_catalog(
                    store=actor_catalog_store or ActorCatalogStore(data_dir / "actor-catalog.json"),
                    source_path=resolved.data_dir / "actor-catalog.json",
                    dry_run=options.dry_run,
                )
                aliases_added = _merge_identity_aliases(
                    store=alias_store or IdentityAliasStore(data_dir / "identity-aliases.json"),
                    source_path=resolved.data_dir / "identity-aliases.json",
                    dry_run=options.dry_run,
                )
                filters_added = _merge_filter_words(
                    store=filter_words_store or FilterWordsStore(data_dir / "filter-words.txt"),
                    source_path=resolved.data_dir / "filter-words.txt",
                    dry_run=options.dry_run,
                )

        if not options.actors_only:
            seed_path = _find_work_seed(resolved)
            if seed_path is not None and repo is not None:
                if options.dry_run:
                    seed_catalog = json.loads(seed_path.read_text(encoding="utf-8-sig"))
                    works_list = seed_catalog.get("works", []) if isinstance(seed_catalog, dict) else []
                    works_created = len(works_list)
                    notes.append(f"dry-run would seed works from {seed_path.name}")
                else:
                    result = seed_non_jav_works(
                        repo,
                        seed_path=seed_path,
                        actor_store=actor_store,
                        actor_images_dir=actor_images_dir,
                        artwork_dir=artwork_dir,
                    )
                    works_created = result.created
                    works_updated = result.updated
                    works_posters = result.posters
                    works_actors_added = result.actors_added
            elif seed_path is None:
                notes.append("no non-jav-works.json seed found in bundle")
            else:
                notes.append("repository unavailable; skipped work seed")

            artwork_copied = _copy_missing_tree(
                resolved.data_dir / "artwork",
                artwork_dir,
                dry_run=options.dry_run,
            )

            if options.include_formal and repo is not None:
                portable_db = resolved.data_dir / "shadow-mdc.db"
                if portable_db.is_file():
                    formal_imported = _import_formal_works_from_portable_db(
                        repo=repo,
                        portable_db=portable_db,
                        source_data_dir=resolved.data_dir,
                        target_artwork_dir=artwork_dir,
                        dry_run=options.dry_run,
                    )
                else:
                    notes.append("no portable shadow-mdc.db in bundle")

        catalog_touched = (
            actors_added
            + actors_updated
            + works_created
            + works_updated
            + formal_imported
            + jav_merged
            + aliases_added
            + filters_added
        )
        if catalog_touched == 0 and (images_copied or artwork_copied):
            notes.append(
                "media-only pack: extracted images/artwork only; merge all split .tar.gz "
                "parts into one folder (or one archive) before import for a full catalog"
            )
        elif catalog_touched == 0 and images_copied == 0 and artwork_copied == 0:
            notes.append(
                "nothing imported; need data/non-jav-actors.json / non-jav-works.json "
                "(or extract+merge split catalog parts first)"
            )

        return CatalogImportResult(
            dry_run=options.dry_run,
            bundle_kind=resolved.kind,
            actors_added=actors_added,
            actors_updated=actors_updated,
            actors_unchanged=actors_unchanged,
            actor_images_copied=images_copied,
            works_created=works_created,
            works_updated=works_updated,
            works_posters=works_posters,
            works_actors_added=works_actors_added,
            artwork_copied=artwork_copied,
            formal_works_imported=formal_imported,
            jav_actors_merged=jav_merged,
            aliases_keys_added=aliases_added,
            filter_words_added=filters_added,
            notes=tuple(notes),
        )


_PLACEHOLDER_BIOGRAPHY_MARKERS = (
    "seeded non-jav performer",
    "designed identicon avatar",
    "placeholder",
)


def _is_placeholder_biography(value: str | None) -> bool:
    text = (value or "").casefold()
    return any(marker in text for marker in _PLACEHOLDER_BIOGRAPHY_MARKERS)


def merge_non_jav_actor_profiles(
    existing: NonJavActorProfile,
    incoming: NonJavActorProfile,
) -> NonJavActorProfile:
    """Merge an imported profile into the local one.

    Aliases and groups are additive (synonyms accumulate). Work-derived categories and the
    provenance ``notes`` follow the incoming bundle — unioning them produced nonsense such
    as ``['China', 'Other', 'Europe']`` and hid freshly linked performers. Free-text
    ``biography`` keeps local edits unless the local value is still a seeding placeholder,
    and the portrait is only adopted when the local profile has none.
    """

    aliases = _unique_casefold((*existing.aliases, *incoming.aliases))
    groups = _unique_casefold((*existing.groups, *incoming.groups))
    categories = tuple(dict.fromkeys(incoming.categories)) or existing.categories
    image_file = existing.image_file or incoming.image_file
    biography: str | None
    if incoming.biography and _is_placeholder_biography(existing.biography):
        biography = incoming.biography
    else:
        biography = existing.biography or incoming.biography
    notes = incoming.notes or existing.notes
    return build_non_jav_actor_profile(
        name=existing.name,
        aliases=aliases,
        groups=groups,
        categories=categories or existing.categories,
        image_file=image_file,
        biography=biography,
        notes=notes,
    )


@contextmanager
def _open_bundle(bundle: Path) -> Iterator[_ResolvedBundle]:
    bundle = bundle.resolve()
    if not bundle.exists():
        raise FileNotFoundError(f"bundle does not exist: {bundle}")
    if bundle.is_dir():
        yield _resolve_bundle_root(bundle)
        return

    suffix = "".join(bundle.suffixes[-2:]).lower() if len(bundle.suffixes) >= 2 else bundle.suffix.lower()
    if suffix not in {".tar.gz", ".tgz"} and bundle.suffix.lower() != ".zip":
        raise ValueError(f"unsupported bundle type: {bundle.name} (use directory, .tar.gz, or .zip)")

    with tempfile.TemporaryDirectory(prefix="shadow-mdc-import-") as temporary:
        scratch = Path(temporary)
        if bundle.suffix.lower() == ".zip" or suffix == ".zip":
            with zipfile.ZipFile(bundle) as archive:
                archive.extractall(scratch)
        else:
            with tarfile.open(bundle, "r:*") as archive:
                archive.extractall(scratch)
        yield _resolve_bundle_root(_unwrap_single_directory(scratch))


def _resolve_bundle_root(root: Path) -> _ResolvedBundle:
    if (root / "manifest.json").is_file() and (root / "data").is_dir():
        return _ResolvedBundle(root=root, data_dir=root / "data", kind="export-bundle")
    if (root / "data").is_dir() and any(
        (root / "data" / name).exists()
        for name in ("non-jav-actors.json", "non-jav-works.json", "shadow-mdc.db", "actor-images", "artwork")
    ):
        kind = "export-bundle" if (root / "manifest.json").is_file() else "local-package"
        return _ResolvedBundle(root=root, data_dir=root / "data", kind=kind)
    if any(
        (root / name).exists()
        for name in ("non-jav-actors.json", "non-jav-works.json", "actor-images", "artwork", "shadow-mdc.db")
    ):
        return _ResolvedBundle(root=root, data_dir=root, kind="local-package")
    # seeds-only package next to data/
    if (root / "seeds" / "non-jav-works.json").is_file():
        data_dir = root / "data" if (root / "data").is_dir() else root
        return _ResolvedBundle(root=root, data_dir=data_dir, kind="local-package")
    raise ValueError(
        f"unrecognized catalog bundle layout under {root}: "
        "expected data/non-jav-actors.json, non-jav-works.json, or export manifest.json"
    )


def _unwrap_single_directory(root: Path) -> Path:
    children = [path for path in root.iterdir() if path.name not in {".DS_Store", "__MACOSX"}]
    if len(children) == 1 and children[0].is_dir():
        return children[0]
    return root


def _find_file(directory: Path, *names: str) -> Path | None:
    for name in names:
        candidate = directory / name
        if candidate.is_file():
            return candidate
    return None


def _find_work_seed(resolved: _ResolvedBundle) -> Path | None:
    candidates = (
        resolved.data_dir / "non-jav-works.json",
        resolved.root / "non-jav-works.json",
        resolved.root / "seeds" / "non-jav-works.json",
        resolved.root / "data" / "non-jav-works.json",
    )
    for candidate in candidates:
        if candidate.is_file():
            return candidate
    return None


def _merge_non_jav_actors(
    *,
    actor_store: NonJavActorCatalogStore,
    source_catalog_path: Path | None,
    source_images_dir: Path,
    target_images_dir: Path,
    dry_run: bool,
) -> tuple[int, int, int, int]:
    if source_catalog_path is None:
        return 0, 0, 0, 0
    incoming = NonJavActorCatalog.model_validate_json(source_catalog_path.read_text(encoding="utf-8-sig"))
    current = actor_store.load()
    by_name = {_normalize(actor.name): actor for actor in current.actors}
    added = updated = unchanged = images_copied = 0
    merged_actors: list[NonJavActorProfile] = list(current.actors)

    for profile in incoming.actors:
        key = _normalize(profile.name)
        existing = by_name.get(key)
        image_name, copied = _copy_actor_image(
            profile=profile,
            source_images_dir=source_images_dir,
            target_images_dir=target_images_dir,
            dry_run=dry_run,
        )
        images_copied += copied
        if image_name and profile.image_file != image_name:
            profile = profile.model_copy(update={"image_file": image_name})
        if existing is None:
            added += 1
            if image_name:
                profile = profile.model_copy(update={"image_file": image_name})
            merged_actors.append(profile)
            by_name[key] = profile
            continue

        enriched = merge_non_jav_actor_profiles(existing, profile)
        if (image_name and not enriched.image_file) or (image_name and enriched.image_file is None):
            enriched = enriched.model_copy(update={"image_file": image_name})
        if enriched == existing:
            unchanged += 1
        else:
            updated += 1
            merged_actors = [actor for actor in merged_actors if _normalize(actor.name) != key]
            merged_actors.append(enriched)
            by_name[key] = enriched

    if not dry_run and (added or updated):
        merged_actors.sort(key=lambda actor: actor.name.casefold())
        actor_store.save(
            current.model_copy(
                update={
                    "actors": tuple(merged_actors),
                    "source": current.source or incoming.source,
                }
            )
        )
    return added, updated, unchanged, images_copied


def _copy_actor_image(
    *,
    profile: NonJavActorProfile,
    source_images_dir: Path,
    target_images_dir: Path,
    dry_run: bool,
) -> tuple[str | None, int]:
    if not profile.image_file:
        return None, 0
    source = source_images_dir / profile.image_file
    if not source.is_file():
        return None, 0
    target = target_images_dir / profile.image_file
    if target.is_file():
        return profile.image_file, 0
    if not dry_run:
        target_images_dir.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
    return profile.image_file, 1


def _copy_missing_tree(source: Path, destination: Path, *, dry_run: bool) -> int:
    if not source.is_dir():
        return 0
    copied = 0
    for path in source.rglob("*"):
        if not path.is_file():
            continue
        relative = path.relative_to(source)
        target = destination / relative
        if target.is_file():
            continue
        copied += 1
        if dry_run:
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
    return copied


def _merge_jav_actor_catalog(*, store: ActorCatalogStore, source_path: Path, dry_run: bool) -> int:
    if not source_path.is_file():
        return 0
    try:
        incoming = ActorCatalogStore(source_path).load()
    except ValueError:
        return 0
    if not incoming:
        return 0
    current = store.load()
    merged = merge_actor_catalogs(current, incoming, IdentityAliasRules())
    added = max(0, len(merged) - len(current))
    if not dry_run and merged != current:
        store.save(merged)
    return added


def _merge_identity_aliases(*, store: IdentityAliasStore, source_path: Path, dry_run: bool) -> int:
    if not source_path.is_file():
        return 0
    try:
        payload = json.loads(source_path.read_text(encoding="utf-8"))
        incoming = IdentityAliasRules.model_validate(payload)
    except (OSError, json.JSONDecodeError, ValidationError):
        return 0
    try:
        current = store.load()
    except ValueError:
        current = default_alias_rules()
    studios = dict(current.studios)
    series = dict(current.series)
    actors = dict(current.actors)
    added = 0
    for mapping, target in (
        (incoming.studios, studios),
        (incoming.series, series),
        (incoming.actors, actors),
    ):
        for key, value in mapping.items():
            if key not in target:
                target[key] = value
                added += 1
    if not dry_run and added:
        store.save(IdentityAliasRules(studios=studios, series=series, actors=actors))
    return added


def _merge_filter_words(*, store: FilterWordsStore, source_path: Path, dry_run: bool) -> int:
    if not source_path.is_file():
        return 0
    try:
        incoming = FilterWordsStore(source_path).load()
        current = store.load()
    except ValueError:
        return 0
    existing = set(current.words)
    added_words = tuple(word for word in incoming.words if word not in existing)
    if not dry_run and added_words:
        store.save(FilterWords(words=(*current.words, *added_words)))
    return len(added_words)


def _import_formal_works_from_portable_db(
    *,
    repo: Repository,
    portable_db: Path,
    source_data_dir: Path,
    target_artwork_dir: Path,
    dry_run: bool,
) -> int:
    """Upsert formal catalog works from export snapshots without touching runtime tables."""

    imported = 0
    with sqlite3.connect(portable_db) as connection:
        connection.row_factory = sqlite3.Row
        tables = {
            row["name"]
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type = 'table'")
        }
        if "source_snapshots" not in tables:
            return 0
        snapshot_cols = {
            row["name"] for row in connection.execute("PRAGMA table_info(source_snapshots)")
        }
        work_id_col = "work_id" if "work_id" in snapshot_cols else "NULL AS work_id"
        rows = connection.execute(
            f"SELECT provider, external_id, payload, {work_id_col} FROM source_snapshots "
            "ORDER BY fetched_at"
        ).fetchall()
        source_works = _load_portable_works(connection) if "works" in tables else {}
        for row in rows:
            try:
                record = ProviderRecord.model_validate(json.loads(row["payload"]))
            except (TypeError, json.JSONDecodeError, ValidationError):
                try:
                    record = ProviderRecord.model_validate(row["payload"])
                except ValidationError:
                    continue
            imported += 1
            if dry_run:
                continue
            work = repo.upsert_provider_record(record, overwrite=False)
            source_work = source_works.get(str(row["work_id"])) if row["work_id"] else None
            if source_work is None and work.primary_code:
                source_work = source_works.get(f"code:{work.primary_code}")
            if source_work is not None:
                merge_portable_work_fields(work, source_work)
                repo._session.flush()
            _attach_copied_artwork(
                repo,
                work_id=work.id,
                source_data_dir=source_data_dir,
                target_artwork_dir=target_artwork_dir,
                seed_hint=record.external_id,
            )
    return imported


_USER_SOURCES = {"manual", "local-manual", "local-path"}


def _json_value(value: object, default: object) -> object:
    if value is None:
        return default
    if isinstance(value, (list, dict)):
        return value
    try:
        return json.loads(str(value))
    except (TypeError, json.JSONDecodeError):
        return default


def _load_portable_works(connection: sqlite3.Connection) -> dict[str, dict[str, object]]:
    """Index exported ``works`` rows by id and ``code:<primary_code>``."""

    cols = {row["name"] for row in connection.execute("PRAGMA table_info(works)")}
    wanted = [
        c
        for c in (
            "id", "primary_code", "title", "original_title", "plot", "original_plot",
            "tags", "field_sources", "field_locks",
        )
        if c in cols
    ]
    if "id" not in wanted:
        return {}
    out: dict[str, dict[str, object]] = {}
    for row in connection.execute(f"SELECT {', '.join(wanted)} FROM works"):
        item = {key: row[key] for key in wanted}
        item["tags"] = _json_value(item.get("tags"), [])
        item["field_sources"] = _json_value(item.get("field_sources"), {})
        item["field_locks"] = _json_value(item.get("field_locks"), [])
        out[str(item["id"])] = item
        code = item.get("primary_code")
        if code:
            out.setdefault(f"code:{code}", item)
    return out


def merge_portable_work_fields(work: Work, source: dict[str, object]) -> bool:
    """Carry custom tags + translations from an exported work row (idempotent).

    * tags: append-only, deduped; never removes; skipped when target locks ``tags``.
    * title/plot: copy the exported translation only when the target is empty or still
      untranslated, and never over a locked or user-edited (manual/local) field.
    * original_title/original_plot: fill only when empty and unlocked.
    """

    locks = set(work.field_locks or [])
    sources = dict(work.field_sources or {})
    src_sources = source.get("field_sources") if isinstance(source.get("field_sources"), dict) else {}
    changed = False

    incoming_tags = source.get("tags") if isinstance(source.get("tags"), list) else []
    if incoming_tags and "tags" not in locks:
        current = list(work.tags or [])
        seen = {str(t) for t in current}
        added = [str(t) for t in incoming_tags if isinstance(t, str) and t and t not in seen and not seen.add(t)]
        if added:
            work.tags = [*current, *added]
            changed = True

    for name in ("title", "plot"):
        original_name = f"original_{name}"
        value = source.get(name)
        if not isinstance(value, str) or not value.strip():
            continue
        src_kind = str(src_sources.get(name, ""))
        if not src_kind.startswith("translation:"):
            continue
        if name in locks or str(sources.get(name, "")) in _USER_SOURCES:
            continue
        current = getattr(work, name)
        target_translated = str(sources.get(name, "")).startswith("translation:")
        if current and target_translated:
            continue
        if current == value:
            continue
        if getattr(work, original_name) is None and original_name not in locks:
            src_original = source.get(original_name)
            setattr(work, original_name, src_original or current or None)
            sources[original_name] = str(src_sources.get(original_name) or sources.get(name, "catalog-import"))
        setattr(work, name, value)
        sources[name] = src_kind
        changed = True

    for original_name in ("original_title", "original_plot"):
        value = source.get(original_name)
        if (
            isinstance(value, str)
            and value
            and getattr(work, original_name) is None
            and original_name not in locks
        ):
            setattr(work, original_name, value)
            sources.setdefault(original_name, str(src_sources.get(original_name) or "catalog-import"))
            changed = True

    if changed:
        work.field_sources = sources
    return changed


def _attach_copied_artwork(
    repo: Repository,
    *,
    work_id: str,
    source_data_dir: Path,
    target_artwork_dir: Path,
    seed_hint: str,
) -> None:
    work = repo.get_work(work_id)
    if work is None:
        return
    if any(
        isinstance(item.get("local_path"), str) and Path(str(item["local_path"])).is_file()
        for item in work.artwork
    ):
        return
    for candidate_id in (work_id, seed_hint):
        source_dir = source_data_dir / "artwork" / candidate_id
        if not source_dir.is_dir():
            continue
        files = sorted(path for path in source_dir.iterdir() if path.is_file())
        if not files:
            continue
        target_dir = target_artwork_dir / work_id
        target_dir.mkdir(parents=True, exist_ok=True)
        poster = next(
            (path for path in files if path.stem.lower().startswith("poster")),
            files[0],
        )
        target = target_dir / poster.name
        if not target.is_file():
            shutil.copy2(poster, target)
        retained = [dict(item) for item in work.artwork if item.get("kind") not in {"poster", "thumb"}]
        work.artwork = [
            {
                "kind": "poster",
                "local_path": str(target),
                "source": "catalog-import",
            },
            *retained,
        ]
        return


def _unique_casefold(values: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    ordered: list[str] = []
    for value in values:
        cleaned = value.strip()
        if not cleaned:
            continue
        key = cleaned.casefold()
        if key in seen:
            continue
        seen.add(key)
        ordered.append(cleaned)
    return tuple(ordered)


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()


__all__ = (
    "CatalogImportRequest",
    "CatalogImportResult",
    "import_catalog_bundle",
    "merge_non_jav_actor_profiles",
)
