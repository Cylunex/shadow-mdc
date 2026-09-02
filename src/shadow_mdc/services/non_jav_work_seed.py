"""Bootstrap curated non-JAV filmography into Work/Actor tables."""

from __future__ import annotations

import hashlib
import shutil
import sys
import unicodedata
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from ..db.models import ExternalIdentity, Work
from ..db.repository import Repository
from ..domain import ProviderRecord
from ..enums import ContentFamily, IdentityKind, MediaCategory
from ..identity import normalize_identity_value
from .non_jav_actor_catalog import (
    NonJavActorCatalogStore,
    build_non_jav_actor_profile,
)


class NonJavSeedActor(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    groups: tuple[str, ...] = ()
    categories: tuple[MediaCategory, ...] = (MediaCategory.OTHER,)
    biography: str | None = None
    notes: str | None = None


class NonJavSeedWork(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    id: str = Field(min_length=1)
    title: str = Field(min_length=1)
    original_title: str | None = None
    code: str | None = None
    family: ContentFamily = ContentFamily.UNKNOWN
    category: MediaCategory = MediaCategory.OTHER
    year: int | None = Field(default=None, ge=1900, le=2100)
    studio: str | None = None
    series: str | None = None
    plot: str | None = None
    actors: tuple[str, ...] = Field(min_length=1)
    tags: tuple[str, ...] = ()
    aliases: tuple[str, ...] = ()
    ensure_actors: tuple[NonJavSeedActor, ...] = ()

    @field_validator("actors")
    @classmethod
    def validate_actors(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        cleaned = tuple(name.strip() for name in value if name.strip())
        if not cleaned:
            raise ValueError("seed work requires at least one actor")
        return cleaned


class NonJavWorkSeedCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    source: str = "curated-non-jav-seed"
    works: tuple[NonJavSeedWork, ...] = ()


@dataclass(frozen=True)
class NonJavWorkSeedResult:
    created: int
    updated: int
    posters: int
    actors_added: int


PROVIDER = "non-jav-seed"


def load_non_jav_work_seed(path: Path) -> NonJavWorkSeedCatalog:
    if not path.is_file():
        return NonJavWorkSeedCatalog()
    try:
        return NonJavWorkSeedCatalog.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read non-JAV work seed: {exc}") from exc


def seed_non_jav_works(
    repo: Repository,
    *,
    seed_path: Path,
    actor_store: NonJavActorCatalogStore,
    actor_images_dir: Path,
    artwork_dir: Path,
) -> NonJavWorkSeedResult:
    """Upsert curated non-JAV works and optional missing actor profiles."""

    catalog = load_non_jav_work_seed(seed_path)
    created = updated = posters = actors_added = 0
    actor_image_index = _actor_image_index(actor_store)

    for seed_actor in _unique_ensure_actors(catalog):
        if actor_store.get(seed_actor.name) is None:
            profile = build_non_jav_actor_profile(
                name=seed_actor.name,
                aliases=seed_actor.aliases,
                groups=seed_actor.groups or ("independent",),
                categories=seed_actor.categories,
                biography=seed_actor.biography,
                notes=seed_actor.notes or "Added from non-JAV work seed.",
            )
            image_name = _ensure_actor_avatar(profile.name, actor_images_dir)
            if image_name is not None:
                profile = profile.model_copy(update={"image_file": image_name})
            actor_store.upsert(profile)
            actor_image_index[_normalize(profile.name)] = profile.image_file
            for alias in profile.aliases:
                actor_image_index[_normalize(alias)] = profile.image_file
            actors_added += 1

    actor_image_index.update(_actor_image_index(actor_store))

    for item in catalog.works:
        record = _to_provider_record(item)
        existed = _find_seed_work(repo, item.id) is not None
        work = repo.upsert_provider_record(record, overwrite=False)
        if existed:
            updated += 1
        else:
            created += 1
        if _attach_poster(
            repo,
            work_id=work.id,
            seed=item,
            actor_images_dir=actor_images_dir,
            artwork_dir=artwork_dir,
            actor_image_index=actor_image_index,
        ):
            posters += 1

    return NonJavWorkSeedResult(
        created=created,
        updated=updated,
        posters=posters,
        actors_added=actors_added,
    )


def _find_seed_work(repo: Repository, external_id: str) -> Work | None:
    identity = repo._session.scalar(
        select(ExternalIdentity).where(
            ExternalIdentity.provider == PROVIDER,
            ExternalIdentity.kind == IdentityKind.PROVIDER_ID.value,
            ExternalIdentity.normalized_value == normalize_identity_value(external_id),
        )
    )
    if identity is None:
        return None
    return repo.get_work(identity.work_id)


def _unique_ensure_actors(catalog: NonJavWorkSeedCatalog) -> tuple[NonJavSeedActor, ...]:
    seen: set[str] = set()
    ordered: list[NonJavSeedActor] = []
    for work in catalog.works:
        for actor in work.ensure_actors:
            key = _normalize(actor.name)
            if key in seen:
                continue
            seen.add(key)
            ordered.append(actor)
    return tuple(ordered)


def _to_provider_record(item: NonJavSeedWork) -> ProviderRecord:
    release = date(item.year, 1, 1) if item.year is not None else None
    tags = tuple(dict.fromkeys((*item.tags, *item.aliases, "non-jav-seed")))
    plot_parts = [part for part in (item.plot, *(f"别名: {alias}" for alias in item.aliases)) if part]
    return ProviderRecord(
        provider=PROVIDER,
        external_id=item.id,
        source_url=None,
        code=item.code,
        title=item.title,
        original_title=item.original_title,
        family=item.family,
        category=item.category,
        release_date=release,
        studio=item.studio,
        series=item.series,
        plot="\n".join(plot_parts) if plot_parts else None,
        actors=item.actors,
        tags=tags,
        artwork=(),
    )


def _actor_image_index(actor_store: NonJavActorCatalogStore) -> dict[str, str | None]:
    index: dict[str, str | None] = {}
    for actor in actor_store.load().actors:
        index[_normalize(actor.name)] = actor.image_file
        for alias in (*actor.aliases, *actor.match_names):
            index.setdefault(_normalize(alias), actor.image_file)
    return index


def _attach_poster(
    repo: Repository,
    *,
    work_id: str,
    seed: NonJavSeedWork,
    actor_images_dir: Path,
    artwork_dir: Path,
    actor_image_index: dict[str, str | None],
) -> bool:
    work = repo.get_work(work_id)
    if work is None:
        return False
    if any(
        isinstance(item.get("local_path"), str) and Path(str(item["local_path"])).is_file()
        for item in work.artwork
    ):
        return False

    source: Path | None = None
    for actor_name in seed.actors:
        image_file = actor_image_index.get(_normalize(actor_name))
        if not image_file:
            continue
        candidate = actor_images_dir / image_file
        if candidate.is_file():
            source = candidate
            break

    target_dir = artwork_dir / work_id
    target_dir.mkdir(parents=True, exist_ok=True)
    if source is None:
        target = target_dir / "poster.png"
        target.write_bytes(_generate_poster_bytes(seed.title, seed.actors[0]))
    else:
        target = target_dir / f"poster{source.suffix.lower() or '.png'}"
        if not target.is_file():
            shutil.copyfile(source, target)

    retained = [dict(item) for item in work.artwork if item.get("kind") not in {"poster", "thumb"}]
    work.artwork = [
        {
            "kind": "poster",
            "local_path": str(target),
            "source": PROVIDER,
            "seed_id": seed.id,
        },
        *retained,
    ]
    return True


def _ensure_actor_avatar(name: str, actor_images_dir: Path) -> str | None:
    actor_images_dir.mkdir(parents=True, exist_ok=True)
    digest = hashlib.sha256(_normalize(name).encode("utf-8")).hexdigest()
    filename = f"{digest}.png"
    path = actor_images_dir / filename
    if path.is_file():
        return filename
    path.write_bytes(_avatar_bytes(name))
    return filename


def _avatar_bytes(name: str) -> bytes:
    scripts = Path(__file__).resolve().parents[3] / "scripts"
    if scripts.is_dir() and str(scripts) not in sys.path:
        sys.path.insert(0, str(scripts))
    try:
        from actor_avatars import avatar_png

        return avatar_png(name)
    except Exception:
        return _generate_poster_bytes(name, name)


def _generate_poster_bytes(title: str, actor: str) -> bytes:
    try:
        from PIL import Image, ImageDraw
    except ImportError:
        return (
            b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
            b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\x0f"
            b"\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00\x00\x00IEND\xaeB`\x82"
        )

    width, height = 480, 640
    digest = hashlib.sha256(_normalize(f"{title}|{actor}").encode("utf-8")).digest()
    base = (20 + digest[0] % 40, 28 + digest[1] % 50, 40 + digest[2] % 60)
    accent = (90 + digest[3] % 120, 120 + digest[4] % 100, 130 + digest[5] % 90)
    image = Image.new("RGB", (width, height), base)
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, height - 220, width, height), fill=accent)
    font_large = _load_font(36)
    font_small = _load_font(24)
    draw.text((28, 40), _clamp_text(actor, 18), fill=(240, 245, 248), font=font_large)
    draw.text((28, height - 180), _clamp_text(title, 28), fill=(12, 16, 18), font=font_small)
    draw.text((28, height - 80), "non-JAV seed", fill=(20, 28, 32), font=font_small)
    buffer = BytesIO()
    image.save(buffer, format="PNG", optimize=True)
    return buffer.getvalue()


def _load_font(size: int):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    for path in (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Bold.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    ):
        candidate = Path(path)
        if candidate.is_file():
            try:
                return ImageFont.truetype(str(candidate), size=size)
            except OSError:
                continue
    return ImageFont.load_default()


def _clamp_text(value: str, limit: int) -> str:
    cleaned = " ".join(value.split())
    return cleaned if len(cleaned) <= limit else f"{cleaned[: max(1, limit - 1)]}…"


def _normalize(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()
