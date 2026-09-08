"""Bootstrap curated FANZA-era yearly top JAV actresses into Work/Actor tables."""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import httpx
from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy import select

from ..db.models import ExternalIdentity, Work
from ..db.repository import Repository
from ..domain import Artwork, ProviderRecord
from ..enums import ContentFamily, IdentityKind, MediaCategory
from ..identity import normalize_identity_value

PROVIDER = "jav-yearly-seed"


class JavSeedWork(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    code: str = Field(min_length=3)
    title: str = Field(min_length=1)
    original_title: str | None = None
    studio: str | None = None
    year: int | None = Field(default=None, ge=1990, le=2100)
    release_date: str | None = None
    cover_url: str | None = None
    plot: str | None = None
    tags: tuple[str, ...] = ()

    @field_validator("code")
    @classmethod
    def normalize_code(cls, value: str) -> str:
        cleaned = value.strip().upper().replace("_", "-")
        match = re.fullmatch(r"([A-Z][A-Z0-9]*)-(\d+)", cleaned)
        if match is None:
            raise ValueError(f"invalid JAV code: {value}")
        return f"{match.group(1)}-{int(match.group(2))}"


class JavSeedActress(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    name: str = Field(min_length=1)
    aliases: tuple[str, ...] = ()
    rank: int = Field(ge=1, le=100)
    works: tuple[JavSeedWork, ...] = Field(min_length=1)


class JavYearBucket(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    year: int = Field(ge=2000, le=2100)
    actresses: tuple[JavSeedActress, ...] = Field(min_length=1)


class JavYearlySeedCatalog(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    version: int = Field(default=1, ge=1)
    source: str = "curated-fanza-annual-consensus"
    notes: str | None = None
    years: tuple[JavYearBucket, ...] = ()


@dataclass(frozen=True)
class JavYearlySeedResult:
    created: int
    updated: int
    posters: int
    actresses: int
    works: int
    years: tuple[int, ...]


def load_jav_yearly_seed(path: Path) -> JavYearlySeedCatalog:
    if not path.is_file():
        return JavYearlySeedCatalog()
    try:
        return JavYearlySeedCatalog.model_validate_json(path.read_text(encoding="utf-8-sig"))
    except (OSError, UnicodeError, ValueError) as exc:
        raise ValueError(f"cannot read JAV yearly seed: {exc}") from exc


def dmm_content_id(code: str) -> str:
    cleaned = code.strip().upper().replace("_", "-")
    match = re.fullmatch(r"([A-Z]+)-(\d+)", cleaned)
    if match is None:
        raise ValueError(f"cannot derive DMM content id from {code}")
    return f"{match.group(1).lower()}{int(match.group(2)):05d}"


def guess_dmm_cover_urls(code: str) -> tuple[str, ...]:
    content_id = dmm_content_id(code)
    return (
        f"https://pics.dmm.co.jp/digital/video/{content_id}/{content_id}pl.jpg",
        f"https://pics.dmm.co.jp/mono/movie/adult/{content_id}/{content_id}pl.jpg",
        f"https://pics.dmm.co.jp/digital/video/1{content_id}/1{content_id}pl.jpg",
    )


def studio_for_code(code: str) -> str | None:
    prefix = code.split("-", 1)[0].upper()
    mapping = {
        "SNIS": "S1 NO.1 STYLE",
        "SSNI": "S1 NO.1 STYLE",
        "SSIS": "S1 NO.1 STYLE",
        "SONE": "S1 NO.1 STYLE",
        "OFJE": "S1 NO.1 STYLE",
        "MIDV": "MOODYZ",
        "MIDE": "MOODYZ",
        "MIAA": "MOODYZ",
        "MIAE": "MOODYZ",
        "IPX": "IdeaPocket",
        "IPZ": "IdeaPocket",
        "PRED": "Premium",
        "STARS": "SOD Create",
        "START": "SOD Create",
        "PPPD": "OPPAI",
        "WANZ": "WANZ FACTORY",
        "HND": "本中",
        "CAWD": "kawaii*",
        "CJOD": "痴女ヘブン",
        "JUFE": "Fitch",
        "JUFD": "Fitch",
        "JUY": "Madonna",
        "JUQ": "Madonna",
        "JUX": "Madonna",
        "BLK": "kira☆kira",
        "MEYD": "溜池ゴロー",
        "ATID": "Attackers",
        "RBK": "Attackers",
        "MVSD": "エムズビデオグループ",
        "ABP": "プレステージ",
        "MMUS": "officE K's",
        "SOE": "S1 NO.1 STYLE",
        "ONED": "S1 NO.1 STYLE",
        "SSNID": "S1 NO.1 STYLE",
        "MIDD": "MOODYZ",
        "MIRD": "MOODYZ",
        "MIGD": "MOODYZ",
        "IPTD": "IdeaPocket",
        "IPZ": "IdeaPocket",
        "PGD": "Premium",
        "PJD": "Premium",
        "STAR": "SOD Create",
        "SDDE": "SOD Create",
        "DV": "アリスJAPAN",
        "SUPD": "Moodyz",
        "KAWD": "kawaii*",
        "RKI": "ROOKIE",
        "PPSD": "OPPAI",
        "EBOD": "E-BODY",
        "TYOD": "乱丸",
        "JUFD": "Fitch",
    }
    return mapping.get(prefix)


def seed_jav_yearly_top(
    repo: Repository,
    *,
    seed_path: Path,
    artwork_dir: Path,
    http_client: httpx.AsyncClient | None = None,
    download_posters: bool = True,
    artwork_max_bytes: int = 25 * 1024 * 1024,
) -> JavYearlySeedResult:
    """Upsert curated yearly-top JAV works into the real Work/Actor model."""

    catalog = load_jav_yearly_seed(seed_path)
    created = updated = posters = 0
    actress_names: set[str] = set()
    work_codes: set[str] = set()
    years = tuple(sorted({bucket.year for bucket in catalog.years}))

    expanded = _expand_unique_works(catalog)
    for item in expanded:
        actress_names.update(item.actress_names)
        work_codes.add(item.work.code)
        record = _to_provider_record(item)
        existed = _find_seed_work(repo, record.external_id) is not None or (
            repo.find_work_by_code(item.work.code) is not None
        )
        work = repo.upsert_provider_record(record, overwrite=False)
        if existed:
            updated += 1
        else:
            created += 1
        if download_posters and _ensure_poster(
            repo,
            work_id=work.id,
            work=item.work,
            artwork_dir=artwork_dir,
            http_client=http_client,
            artwork_max_bytes=artwork_max_bytes,
        ):
            posters += 1

    return JavYearlySeedResult(
        created=created,
        updated=updated,
        posters=posters,
        actresses=len(actress_names),
        works=len(work_codes),
        years=years,
    )


@dataclass(frozen=True)
class _ExpandedWork:
    actress_names: tuple[str, ...]
    aliases: tuple[str, ...]
    year_tags: tuple[str, ...]
    ranks: tuple[str, ...]
    work: JavSeedWork


def _expand_unique_works(catalog: JavYearlySeedCatalog) -> tuple[_ExpandedWork, ...]:
    by_code: dict[str, _ExpandedWork] = {}
    for bucket in catalog.years:
        year_tag = f"jav-top-{bucket.year}"
        for actress in bucket.actresses:
            rank_tag = f"jav-top-{bucket.year}-rank-{actress.rank}"
            for work in actress.works:
                existing = by_code.get(work.code)
                if existing is None:
                    by_code[work.code] = _ExpandedWork(
                        actress_names=(actress.name,),
                        aliases=actress.aliases,
                        year_tags=(year_tag,),
                        ranks=(rank_tag,),
                        work=work,
                    )
                    continue
                year_tags = tuple(dict.fromkeys((*existing.year_tags, year_tag)))
                ranks = tuple(dict.fromkeys((*existing.ranks, rank_tag)))
                aliases = tuple(dict.fromkeys((*existing.aliases, *actress.aliases)))
                actress_names = tuple(dict.fromkeys((*existing.actress_names, actress.name)))
                merged_work = existing.work
                updates: dict[str, object] = {}
                if not merged_work.cover_url and work.cover_url:
                    updates["cover_url"] = work.cover_url
                if not merged_work.studio and work.studio:
                    updates["studio"] = work.studio
                if not merged_work.original_title and work.original_title:
                    updates["original_title"] = work.original_title
                if not merged_work.release_date and work.release_date:
                    updates["release_date"] = work.release_date
                if updates:
                    merged_work = merged_work.model_copy(update=updates)
                by_code[work.code] = _ExpandedWork(
                    actress_names=actress_names,
                    aliases=aliases,
                    year_tags=year_tags,
                    ranks=ranks,
                    work=merged_work,
                )
    return tuple(by_code.values())


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


def _to_provider_record(item: _ExpandedWork) -> ProviderRecord:
    work = item.work
    release = _parse_release(work.release_date, work.year)
    studio = work.studio or studio_for_code(work.code)
    tags = tuple(
        dict.fromkeys(
            (
                *work.tags,
                *item.year_tags,
                *item.ranks,
                "jav-yearly-seed",
                "jav",
            )
        )
    )
    cover = work.cover_url or guess_dmm_cover_urls(work.code)[0]
    artwork = (
        (Artwork.model_validate({"url": cover, "kind": "poster"}),)
        if cover.startswith(("http://", "https://"))
        else ()
    )
    return ProviderRecord(
        provider=PROVIDER,
        external_id=f"jav-yearly:{work.code}",
        source_url=None,
        code=work.code,
        title=work.title,
        original_title=work.original_title or work.title,
        family=ContentFamily.JAV,
        category=MediaCategory.JAPAN,
        release_date=release,
        studio=studio,
        plot=work.plot,
        actors=item.actress_names,
        tags=tags,
        artwork=artwork,
        language="ja",
    )


def _parse_release(value: str | None, year: int | None) -> date | None:
    if value:
        try:
            return date.fromisoformat(value[:10])
        except ValueError:
            pass
    if year is not None:
        return date(year, 1, 1)
    return None


def _ensure_poster(
    repo: Repository,
    *,
    work_id: str,
    work: JavSeedWork,
    artwork_dir: Path,
    http_client: httpx.AsyncClient | None,
    artwork_max_bytes: int,
) -> bool:
    stored = repo.get_work(work_id)
    if stored is None:
        return False
    if any(
        isinstance(item.get("local_path"), str) and Path(str(item["local_path"])).is_file()
        for item in stored.artwork
    ):
        return False

    # Prefer synchronous download so the seed script stays simple.
    urls = []
    if work.cover_url:
        urls.append(work.cover_url)
    urls.extend(guess_dmm_cover_urls(work.code))
    urls = list(dict.fromkeys(urls))

    target_dir = artwork_dir / work_id
    target_dir.mkdir(parents=True, exist_ok=True)
    downloaded: Path | None = None
    used_url: str | None = None
    for url in urls:
        try:
            path = _download_sync(url, target_dir, max_bytes=artwork_max_bytes, client=http_client)
        except (OSError, httpx.HTTPError, ValueError):
            continue
        if path is not None:
            downloaded = path
            used_url = url
            break
    if downloaded is None or used_url is None:
        return False

    retained = [dict(item) for item in stored.artwork if item.get("kind") not in {"poster", "thumb"}]
    stored.artwork = [
        {
            "kind": "poster",
            "url": used_url,
            "local_path": str(downloaded),
            "source": PROVIDER,
            "seed_code": work.code,
        },
        *retained,
    ]
    return True


def _download_sync(
    url: str,
    target_dir: Path,
    *,
    max_bytes: int,
    client: httpx.AsyncClient | None,
) -> Path | None:
    existing = next(target_dir.glob("poster.*"), None)
    if existing is not None and existing.is_file():
        return existing

    headers = {"User-Agent": "ShadowMDC/0.1 (+https://github.com/Cylunex/shadow-mdc)"}
    if client is not None:
        # Sync fallback via httpx Client to avoid requiring an event loop here.
        pass
    with httpx.Client(timeout=30, follow_redirects=True, headers=headers) as sync_client:
        response = sync_client.get(url)
        response.raise_for_status()
        content = response.content
        if len(content) > max_bytes or len(content) < 1024:
            raise ValueError("unexpected artwork size")
        content_type = response.headers.get("content-type", "").split(";")[0].strip().lower()
        extension = {
            "image/jpeg": ".jpg",
            "image/png": ".png",
            "image/webp": ".webp",
            "image/gif": ".gif",
        }.get(content_type)
        if extension is None:
            if content.startswith(b"\xff\xd8\xff"):
                extension = ".jpg"
            elif content.startswith(b"\x89PNG"):
                extension = ".png"
            else:
                extension = ".jpg"
        destination = target_dir / f"poster{extension}"
        destination.write_bytes(content)
        return destination


def normalize_actor_key(value: str) -> str:
    return unicodedata.normalize("NFKC", value).casefold().strip()
