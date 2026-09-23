"""Fill empty Actor.image_url rows from GFriends portraits.

Downloads optional local copies under ``data/actor-images/`` and points
``image_url`` at ``/api/actor-images/<file>`` so the catalog UI does not
depend on GitHub/jsDelivr from the browser. Never invents placeholders.
"""

from __future__ import annotations

import hashlib
import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

import httpx
from sqlalchemy import or_, select

from ..db.models import Actor
from ..db.repository import Repository
from .gfriends import GfriendsActorImageResolver

logger = logging.getLogger(__name__)

_MIN_IMAGE_BYTES = 1200


@dataclass(frozen=True, slots=True)
class GfriendsFillStats:
    scanned: int
    matched: int
    filled: int
    skipped_has_image: int
    skipped_no_match: int
    downloaded: int
    failed: int
    dry_run: bool
    filetree_source: str
    filetree_entries: int


def _detect_image_ext(content: bytes) -> str | None:
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if content.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if content.startswith(b"RIFF") and b"WEBP" in content[:16]:
        return ".webp"
    if content.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    return None


def _image_filename(name: str, extension: str) -> str:
    digest = hashlib.sha256(
        unicodedata.normalize("NFKC", name).casefold().strip().encode("utf-8")
    ).hexdigest()
    if not extension.startswith("."):
        extension = f".{extension}"
    return f"gfriends-{digest}{extension}"


def _candidate_names(actor: Actor) -> list[str]:
    names: list[str] = [actor.name]
    for alias in actor.aliases or []:
        if isinstance(alias, str) and alias.strip():
            names.append(alias.strip())
    # Deduplicate while preserving order.
    seen: set[str] = set()
    ordered: list[str] = []
    for name in names:
        key = unicodedata.normalize("NFKC", name).casefold().strip()
        if not key or key in seen:
            continue
        seen.add(key)
        ordered.append(name)
    return ordered


_CJK_RE = re.compile(r"[\u3040-\u30ff\u4e00-\u9fff]")


def list_actors_missing_images(repo: Repository, *, limit: int | None = None) -> list[Actor]:
    """Actors with empty image_url, CJK names first (GFriends is JP-centric)."""

    statement = select(Actor).where(or_(Actor.image_url.is_(None), Actor.image_url == ""))
    actors = list(repo._session.scalars(statement))
    actors.sort(key=lambda actor: (0 if _CJK_RE.search(actor.name or "") else 1, actor.name or ""))
    if limit is not None:
        return actors[: max(limit, 0)]
    return actors


def fill_actor_images_from_gfriends(
    repo: Repository,
    resolver: GfriendsActorImageResolver,
    *,
    actor_images_dir: Path,
    download: bool = True,
    dry_run: bool = False,
    limit: int | None = None,
    force_refresh_index: bool = False,
    http_client: httpx.Client | None = None,
    commit_every: int = 25,
) -> GfriendsFillStats:
    """Match actors with empty ``image_url`` against GFriends and persist URLs."""

    refresh_stats = resolver.refresh(force=force_refresh_index)
    actors = list_actors_missing_images(repo, limit=limit)
    owns_client = http_client is None
    client = http_client or httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc; gfriends-fill)"},
        follow_redirects=True,
    )
    matched = filled = downloaded = failed = 0
    skipped_no_match = 0
    try:
        actor_images_dir.mkdir(parents=True, exist_ok=True)
        for actor in actors:
            remote_url = resolver.resolve(_candidate_names(actor))
            if remote_url is None:
                skipped_no_match += 1
                continue
            matched += 1
            if dry_run:
                filled += 1
                continue
            image_url = remote_url
            if download:
                try:
                    local_name = _download_portrait(client, remote_url, actor.name, actor_images_dir)
                    image_url = f"/api/actor-images/{local_name}"
                    downloaded += 1
                except Exception as exc:
                    logger.warning("gfriends download failed for %s: %s", actor.name, exc)
                    failed += 1
                    # Still store CDN URL so UI can try remote.
            actor.image_url = image_url
            filled += 1
            if not dry_run and commit_every > 0 and filled % commit_every == 0:
                repo._session.commit()
        if not dry_run and filled:
            repo._session.flush()
    finally:
        if owns_client:
            client.close()

    return GfriendsFillStats(
        scanned=len(actors),
        matched=matched,
        filled=filled,
        skipped_has_image=0,
        skipped_no_match=skipped_no_match,
        downloaded=downloaded,
        failed=failed,
        dry_run=dry_run,
        filetree_source=str(refresh_stats.get("source", "")),
        filetree_entries=int(refresh_stats.get("entries", 0)),
    )


def _download_portrait(
    client: httpx.Client,
    url: str,
    actor_name: str,
    actor_images_dir: Path,
) -> str:
    response = client.get(url)
    response.raise_for_status()
    content = response.content
    if len(content) < _MIN_IMAGE_BYTES:
        raise ValueError(f"image too small ({len(content)} bytes)")
    extension = _detect_image_ext(content)
    if extension is None:
        raise ValueError("unrecognized image bytes")
    filename = _image_filename(actor_name, extension)
    path = actor_images_dir / filename
    if not path.exists() or path.stat().st_size != len(content):
        tmp = path.with_suffix(path.suffix + ".part")
        tmp.write_bytes(content)
        tmp.replace(path)
    return filename


def localize_cdn_actor_images(
    repo: Repository,
    *,
    actor_images_dir: Path,
    cdn_prefix: str = "https://cdn.jsdelivr.net/gh/gfriends/",
    limit: int | None = None,
    dry_run: bool = False,
    commit_every: int = 25,
    http_client: httpx.Client | None = None,
) -> GfriendsFillStats:
    """Download remote GFriends CDN ``image_url`` values into local actor-images."""

    _ = cdn_prefix  # reserved for future CDN allowlists
    statement = (
        select(Actor)
        .where(Actor.image_url.is_not(None))
        .where(Actor.image_url.like("https://%gfriends%"))
        .order_by(Actor.name)
    )
    actors = list(repo._session.scalars(statement))
    actors.sort(key=lambda actor: (0 if _CJK_RE.search(actor.name or "") else 1, actor.name or ""))
    if limit is not None:
        actors = actors[: max(limit, 0)]

    owns_client = http_client is None
    client = http_client or httpx.Client(
        timeout=30.0,
        headers={"User-Agent": "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc; gfriends-localize)"},
        follow_redirects=True,
    )
    filled = downloaded = failed = 0
    try:
        actor_images_dir.mkdir(parents=True, exist_ok=True)
        for actor in actors:
            remote = actor.image_url or ""
            if not remote.startswith("https://"):
                continue
            if dry_run:
                filled += 1
                continue
            try:
                local_name = _download_portrait(client, remote, actor.name, actor_images_dir)
                actor.image_url = f"/api/actor-images/{local_name}"
                downloaded += 1
                filled += 1
                if commit_every > 0 and filled % commit_every == 0:
                    repo._session.commit()
            except Exception as exc:
                logger.warning("gfriends localize failed for %s: %s", actor.name, exc)
                failed += 1
        if not dry_run and filled:
            repo._session.flush()
    finally:
        if owns_client:
            client.close()

    return GfriendsFillStats(
        scanned=len(actors),
        matched=len(actors),
        filled=filled,
        skipped_has_image=0,
        skipped_no_match=0,
        downloaded=downloaded,
        failed=failed,
        dry_run=dry_run,
        filetree_source="localize",
        filetree_entries=0,
    )
