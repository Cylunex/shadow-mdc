"""Reverse-import existing movie.nfo sidecars into Work records."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

from pydantic import BaseModel, ConfigDict, Field

from ..db.models import MediaAsset, Work
from ..db.repository import Repository
from ..domain import ProviderRecord
from ..enums import ContentFamily, MediaCategory
from ..identity import extract_code, normalize_identity_value

_CODE_TAG = re.compile(r"(?i)^(?:num|id|code|jav)$")


class ParsedNfo(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    title: str | None = None
    original_title: str | None = None
    plot: str | None = None
    code: str | None = None
    studio: str | None = None
    series: str | None = None
    premiered: str | None = None
    actors: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    unique_ids: dict[str, str] = Field(default_factory=dict)
    source_path: str


class NfoImportPolicy(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    mode: str = "candidate"  # trust | candidate | ignore


class NfoImportResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scanned: int
    trusted: int
    candidates: int
    ignored: int
    errors: tuple[str, ...] = ()


@dataclass(slots=True)
class NfoImporter:
    repository: Repository
    policy: NfoImportPolicy = NfoImportPolicy()

    def import_library_assets(self, assets: list[MediaAsset]) -> NfoImportResult:
        scanned = trusted = candidates = ignored = 0
        errors: list[str] = []
        for asset in assets:
            nfo_path = Path(asset.path).with_name("movie.nfo")
            if not nfo_path.is_file():
                # Also accept sibling named after media stem.
                alt = Path(asset.path).with_suffix(".nfo")
                nfo_path = alt if alt.is_file() else nfo_path
            if not nfo_path.is_file():
                continue
            scanned += 1
            if self.policy.mode == "ignore":
                ignored += 1
                continue
            try:
                parsed = parse_movie_nfo(nfo_path)
            except (OSError, ElementTree.ParseError, ValueError) as exc:
                errors.append(f"{nfo_path}: {exc}")
                continue
            record = _to_provider_record(parsed)
            if self.policy.mode == "trust":
                work = self.repository.upsert_provider_record(record, overwrite=False)
                self.repository.attach_asset_to_work(asset, work)
                trusted += 1
            else:
                from ..domain import MatchEvidence, ScoredCandidate
                from ..enums import MatchDecision

                scored = ScoredCandidate(
                    record=record,
                    score=0.55,
                    decision=MatchDecision.REVIEW,
                    evidence=(MatchEvidence(kind="nfo", contribution=0.55, detail=str(nfo_path)),),
                )
                self.repository.save_candidates(asset, [scored])
                candidates += 1
        return NfoImportResult(
            scanned=scanned,
            trusted=trusted,
            candidates=candidates,
            ignored=ignored,
            errors=tuple(errors[:50]),
        )


def parse_movie_nfo(path: Path) -> ParsedNfo:
    tree = ElementTree.parse(path)
    root = tree.getroot()
    if root is None:
        raise ValueError("empty nfo")
    title = _text(root, "title")
    original = _text(root, "originaltitle")
    plot = _text(root, "plot")
    studio = _text(root, "studio")
    series = _text(root, "set") or _text(root, "showtitle")
    premiered = _text(root, "premiered") or _text(root, "releasedate")
    code = _text(root, "id")
    actors = tuple(
        name
        for actor in root.findall("actor")
        if (name := (actor.findtext("name") or "").strip())
    )
    tags = tuple(
        dict.fromkeys(
            value.strip()
            for tag in list(root.findall("tag")) + list(root.findall("genre"))
            if (value := (tag.text or ""))
        )
    )
    unique_ids: dict[str, str] = {}
    for node in root.findall("uniqueid"):
        value = (node.text or "").strip()
        kind = (node.attrib.get("type") or "").strip() or "provider"
        if value:
            unique_ids[kind] = value
            if _CODE_TAG.match(kind) and not code:
                code = value
    if code:
        extracted = extract_code(code)
        code = extracted or code.strip().upper()
    if not title and not code:
        raise ValueError("nfo missing title and id")
    return ParsedNfo(
        title=title,
        original_title=original,
        plot=plot,
        code=code,
        studio=studio,
        series=series,
        premiered=premiered,
        actors=actors,
        tags=tags,
        unique_ids=unique_ids,
        source_path=str(path.resolve()),
    )


def _to_provider_record(parsed: ParsedNfo) -> ProviderRecord:
    from datetime import date

    release: date | None = None
    if parsed.premiered:
        try:
            release = date.fromisoformat(parsed.premiered[:10])
        except ValueError:
            release = None
    family = ContentFamily.JAV if parsed.code else ContentFamily.UNKNOWN
    category = MediaCategory.JAPAN if parsed.code else MediaCategory.OTHER
    external_id = parsed.code or normalize_identity_value(parsed.title or parsed.source_path)
    return ProviderRecord(
        provider="nfo-import",
        external_id=external_id,
        code=parsed.code,
        title=parsed.title or parsed.code or "Untitled",
        original_title=parsed.original_title,
        plot=parsed.plot,
        family=family,
        category=category,
        studio=parsed.studio,
        series=parsed.series,
        release_date=release,
        actors=parsed.actors,
        tags=parsed.tags,
        source_url=None,
    )


def _text(root: ElementTree.Element, tag: str) -> str | None:
    value = root.findtext(tag)
    if value is None:
        return None
    cleaned = value.strip()
    return cleaned or None
