"""115 → local Emby-style STRM export.

Re-implemented from the *ideas* noted in docs/references (Miyabi GPL-3: ideas only,
no code; openStrm MIT patterns for the 302 gateway):

* Export layout (multi-level, Emby-friendly): ``{root}/{studio}/{CODE}/`` containing
  ``{CODE}.strm`` (or ``{CODE}-cdN.strm``), ``movie.nfo``, ``poster.*`` / ``fanart.*``,
  and ``.shadow-strm.json``. Studio segment reuses the same sanitization as the
  organize templates (``Unknown Studio`` when missing). Configurable via
  ``strm_layout_template`` (default ``{studio}/{code}``).
* Export order per title: poster/fanart → NFO → ``.strm`` last, each written via
  same-directory temp + rename (never unlink first; failed write keeps prior file).
* Code-correction / obsolete cleanup deletes only generated names
  (``movie.nfo`` / ``{stem}.nfo`` / ``{stem}.strm`` / ``{stem}-cdN.strm`` /
  poster/fanart / ``.shadow-strm.json``); user custom files stay.
* ``.strm`` body is either our relay URL ``{public}/api/strm/play/{file_id}[?token=]``
  (302 → 115 direct link) or the legacy OpenList ``{prefix}/{remote path}`` form.
* A small sidecar (``.shadow-strm.json``) records file ids so token / public URL
  rotation can rewrite ``.strm`` bodies in place and delete reconciliation knows
  which 115 files back each folder.
* Content-equal incremental writes: a file whose bytes would not change is left
  untouched (no rename, no mtime bump), so a no-op re-export never wakes Emby's
  realtime monitor and the poller skips the Library/Media/Updated notify.
* Hard config gate: relay mode refuses to export / rewrite / migrate while a
  required setting (export root, public base URL, Emby path mapping when Emby
  notify is on) is missing, instead of inventing defaults.
* OpenList backend: entries are keyed by the absolute OpenList path (``file_id``
  starts with ``/``) and the body is ``{openlist}/d{path}[?sign=…]``, or the relay
  ``{public}/api/strm/openlist{path}[?token=]`` which 302s to that ``/d`` link.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from urllib.parse import quote, urlencode, urlsplit

from ..db.models import ExternalIdentity, Work
from ..media.nfo import build_nfo, merge_export_nfo, nfo_export_fields, write_nfo
from ..media.strm import write_strm
from .openlist import build_openlist_d_url
from .pan import PanSettings, RemoteVideo, SourceFingerprint

SIDECAR_NAME = ".shadow-strm.json"
_EXPORT_LOCK = RLock()
_export_revision = 0
_SAMPLE_RE = re.compile(r"(?i)(?:^|[ ._-])(?:sample|trailer|preview)(?:$|[ ._-])")
RELAY_PATH = "/api/strm/play/"
OPENLIST_RELAY_PATH = "/api/strm/openlist"
_RELAY_RE = re.compile(r"/api/strm/play/(?P<fid>[A-Za-z0-9_-]{1,64})(?:[?#]|$)")
_PART_RE = re.compile(r"(?i)(?:^|[-_ .\[(])(?:cd|part|pt|disc|disk)[-_ .]?(\d{1,2})(?=\D|$)")
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]')


@dataclass(frozen=True, slots=True)
class StrmEntry:
    name: str  # e.g. ABC-123.strm / ABC-123-cd2.strm
    file_id: str
    pick_code: str | None = None
    remote_path: str | None = None
    # OpenList backend: /d sign captured at export time (used when signing is on).
    sign: str | None = None

    @property
    def is_openlist(self) -> bool:
        return self.file_id.startswith("/")


class StrmConfigError(ValueError):
    """Export / rewrite refused because required STRM settings are missing."""

    def __init__(self, problems: Sequence[str]) -> None:
        self.problems = list(problems)
        super().__init__("STRM settings incomplete: " + "; ".join(self.problems))


def strm_config_problems(settings: PanSettings, *, emby_notify: bool = False) -> list[str]:
    """Human-readable list of missing / invalid settings needed to write a library.

    Relay mode needs an export root and an absolute http(s) public base URL that
    Emby can reach; when Emby notify is on, the Emby-side path of the export root
    is required too (otherwise notifies would name paths Emby cannot see).
    """

    problems: list[str] = []
    if not (settings.strm_output_root or "").strip():
        problems.append("STRM output directory (strm_output_root) is not set")
    if settings.strm_mode == "relay":
        base = (settings.strm_public_base_url or "").strip()
        if not base:
            problems.append("public base URL (strm_public_base_url) is required in relay mode")
        else:
            parsed = urlsplit(base)
            if parsed.scheme not in {"http", "https"} or not parsed.netloc:
                problems.append("public base URL (strm_public_base_url) must be an absolute http(s) URL")
    elif settings.pan_backend == "openlist":
        if not (settings.openlist_strm_base_url or settings.openlist_base_url):
            problems.append("OpenList base URL is required for OpenList /d STRM links")
    elif not (settings.strm_url_prefix or "").strip():
        problems.append("STRM URL prefix (strm_url_prefix) is required in OpenList-prefix mode")
    if emby_notify and not (settings.strm_emby_root or "").strip():
        problems.append(
            "Emby path mapping (strm_emby_root) is required while Emby notify is enabled "
            "(set it to the export directory as Emby sees it)"
        )
    return problems


def require_strm_config(settings: PanSettings, *, emby_notify: bool = False) -> None:
    problems = strm_config_problems(settings, emby_notify=emby_notify)
    if problems:
        raise StrmConfigError(problems)


@dataclass(slots=True)
class ExportResult:
    directory: Path
    strm_paths: list[Path] = field(default_factory=list)
    nfo_path: Path | None = None
    artwork_paths: list[Path] = field(default_factory=list)
    # Files actually (re)written or removed; empty → the export was a no-op.
    changed_paths: list[Path] = field(default_factory=list)
    created: bool = False

    @property
    def changed(self) -> bool:
        return bool(self.changed_paths)


@dataclass(slots=True)
class RewriteResult:
    scanned: int = 0
    rewritten: list[Path] = field(default_factory=list)
    skipped: int = 0


@dataclass(slots=True)
class MigrateResult:
    """Export-root change: folders moved old → new root, then bodies rewritten."""

    old_root: Path | None = None
    new_root: Path | None = None
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    conflicts: list[Path] = field(default_factory=list)
    failed: list[Path] = field(default_factory=list)
    rewrite: RewriteResult = field(default_factory=RewriteResult)


@dataclass(slots=True)
class ReconcileResult:
    checked: int = 0
    removed: list[Path] = field(default_factory=list)
    kept: int = 0
    unknown: int = 0
    # Incremental cleanup inside managed folders: .strm not listed in the sidecar.
    orphan_strm_removed: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class RematerializeResult:
    """Layout-template change: managed folders moved to the new relative path."""

    scanned: int = 0
    moved: list[tuple[Path, Path]] = field(default_factory=list)
    skipped: int = 0
    conflicts: list[Path] = field(default_factory=list)
    failed: list[Path] = field(default_factory=list)
    nfo_written: int = 0
    artwork_written: int = 0


def safe_stem(value: str) -> str:
    cleaned = _UNSAFE.sub("_", value).strip().strip(".")
    return cleaned[:120] or "untitled"


DEFAULT_STRM_LAYOUT = "{studio}/{code}"


def _layout_group(category: str | None) -> str:
    return {
        "Japan": "JAV",
        "China": "国产",
        "Korea": "韩国",
        "Europe": "欧美",
        "Other": "其他",
    }.get(category or "", "其他")


def _layout_subgroup(work: Work | None, code: str) -> str:
    if work is None:
        return "影片"
    primary = getattr(work, "primary_code", None) or code
    title = getattr(work, "title", None) or ""
    tags = getattr(work, "tags", None) or []
    if not isinstance(tags, (list, tuple)):
        tags = []
    text = " ".join((str(primary), str(title), *[str(tag) for tag in tags])).casefold()
    category = getattr(work, "category", None)
    if category == "Japan":
        if str(primary).upper().startswith("FC2-"):
            return "FC2"
        uncensored = ("无码", "無碼", "uncensored", "heyzo", "1pondo", "carib", "10musume")
        return "无码" if any(marker in text for marker in uncensored) else "有码"
    if category == "China":
        for marker, name in (("麻豆", "麻豆"), ("探花", "探花"), ("91", "91"), ("自拍", "自拍")):
            if marker in text:
                return name
        return "其他"
    return "影片"


def export_relative_dir(
    code: str,
    work: Work | None = None,
    *,
    template: str | None = None,
) -> Path:
    """Relative export folder under ``strm_output_root`` (never flat at the root).

    Default ``{studio}/{code}`` → e.g. ``SODクリエイト/STARS-145/``. Placeholders:
    ``studio``, ``code``, ``group`` (JAV/国产/…), ``subgroup`` (有码/无码/…).
    """

    stem = safe_stem(code)
    studio_raw = getattr(work, "studio", None) if work is not None else None
    studio = safe_stem(studio_raw if isinstance(studio_raw, str) and studio_raw.strip() else "Unknown Studio")
    category = getattr(work, "category", None) if work is not None else None
    values = {
        "studio": studio,
        "code": stem,
        "group": safe_stem(_layout_group(category if isinstance(category, str) else None)),
        "subgroup": safe_stem(_layout_subgroup(work, code)),
    }
    raw = (template or DEFAULT_STRM_LAYOUT).strip() or DEFAULT_STRM_LAYOUT
    try:
        rendered = raw.format_map(values)
    except KeyError as exc:
        raise ValueError(f"unknown strm_layout_template field: {exc.args[0]}") from exc
    relative = Path(rendered)
    if relative.is_absolute() or ".." in relative.parts:
        raise ValueError("strm_layout_template must be a relative path without '..'")
    parts = [safe_stem(part) for part in relative.parts if part not in {"", "."}]
    if not parts:
        raise ValueError("strm_layout_template rendered empty")
    # Guarantee at least studio/code depth when the template collapses to one segment.
    if len(parts) == 1 and parts[0] == stem:
        parts = [studio, stem]
    return Path(*parts)


def build_relay_locator(public_base_url: str, file_id: str, token: str | None = None) -> str:
    base = public_base_url.strip().rstrip("/")
    if not base:
        raise ValueError("strm_public_base_url is required for relay mode")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("strm_public_base_url must be an absolute http(s) URL")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,64}", file_id):
        raise ValueError("invalid 115 file id")
    url = f"{base}{RELAY_PATH}{file_id}"
    if token:
        url = f"{url}?{urlencode({'token': token})}"
    return url


def build_openlist_locator(prefix: str, remote_path: str) -> str:
    # Same shape as the legacy writer (unencoded path under the OpenList /d prefix).
    base = prefix.rstrip("/")
    rel = remote_path.lstrip("/")
    return f"{base}/{rel}" if rel else base


def build_openlist_relay_locator(public_base_url: str, path: str, token: str | None = None) -> str:
    base = public_base_url.strip().rstrip("/")
    if not base:
        raise ValueError("strm_public_base_url is required for relay mode")
    parsed = urlsplit(base)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise ValueError("strm_public_base_url must be an absolute http(s) URL")
    if not path.startswith("/"):
        raise ValueError("OpenList path must be absolute")
    url = f"{base}{OPENLIST_RELAY_PATH}{quote(path, safe='/')}"
    if token:
        url = f"{url}?{urlencode({'token': token})}"
    return url


def openlist_locator(settings: PanSettings, entry: StrmEntry) -> str:
    path = entry.file_id
    if settings.strm_mode == "relay":
        return build_openlist_relay_locator(settings.strm_public_base_url or "", path, settings.strm_token)
    base = settings.openlist_strm_base_url or settings.openlist_base_url
    if not base:
        raise ValueError("openlist_base_url is required for OpenList STRM")
    if settings.openlist_strm_sign and not entry.sign:
        raise ValueError("OpenList signing is on but no sign is known for this entry")
    return build_openlist_d_url(base, path, entry.sign if settings.openlist_strm_sign else None)


def parse_relay_file_id(locator: str) -> str | None:
    match = _RELAY_RE.search(locator.strip())
    return match.group("fid") if match else None


def locator_for(settings: PanSettings, entry: StrmEntry) -> str:
    if entry.is_openlist:
        return openlist_locator(settings, entry)
    if settings.strm_mode == "relay":
        return build_relay_locator(settings.strm_public_base_url or "", entry.file_id, settings.strm_token)
    remote = entry.remote_path or entry.name.removesuffix(".strm")
    return build_openlist_locator(settings.strm_url_prefix, remote)


def detect_part_number(name: str) -> int | None:
    """Numeric CD/PART marker, or lettered volume A=1…Z when present."""

    stem = Path(name).stem
    match = _PART_RE.search(stem)
    if match:
        return int(match.group(1))
    from ..media.parts import parse_letter_volume

    volume = parse_letter_volume(stem)
    return volume.index if volume is not None else None


def plan_strm_entries(code: str, videos: Sequence[RemoteVideo]) -> list[StrmEntry]:
    """Group by explicit part markers and by the release name around each marker."""
    stem = safe_stem(code)
    items = sorted({item.file_id: item for item in videos}.values(),
                   key=lambda item: (item.name.casefold(), item.file_id))
    if any(not _SAMPLE_RE.search(Path(item.name).stem) for item in items):
        items = [item for item in items if not _SAMPLE_RE.search(Path(item.name).stem)]
    groups: dict[str, list[tuple[RemoteVideo, int | None]]] = {}
    for item in items:
        name = Path(item.name).stem
        part = detect_part_number(item.name)
        marker = _PART_RE.search(name)
        if marker and part:
            release = (name[:marker.start()] + name[marker.end():]).strip(" -_.").casefold()
        elif part:
            release = name[:-1].rstrip(" -_.").casefold()
        else:
            release = ""
        groups.setdefault(release, []).append((item, part))
    result = []
    single_stack = len(groups) == 1
    for group in groups.values():
        numbers = sorted(part for _, part in group if part)
        multipart = len(numbers) > 1 and len(group) == len(numbers) and numbers == list(range(1, len(group) + 1))
        ordered = sorted(group, key=lambda entry: entry[1] or 0) if multipart else group
        group_suffix = ""
        if multipart and not single_stack:
            key = "\0".join(sorted(item.file_id for item, _ in group))
            group_suffix = "-version-" + hashlib.sha256(key.encode()).hexdigest()[:12]
        for item, part in ordered:
            if multipart:
                name = f"{stem}{group_suffix}-cd{part}.strm"
            elif len(items) == 1 and not _PART_RE.search(Path(item.name).stem):
                name = f"{stem}.strm"
            else:
                key = hashlib.sha256(item.file_id.encode()).hexdigest()[:12]
                label = _PART_RE.sub("", Path(item.name).stem).strip(" ._-")
                name = f"{stem} - {safe_stem(label)[:48]} [{key}].strm"
            result.append(StrmEntry(name, item.file_id, item.pick_code, item.relative_path or item.name, item.sign))
    return result


def _atomic_copy(source: Path, destination: Path) -> None:
    from ..media.atomicfile import copy_file

    copy_file(source, destination)


def _same_bytes(path: Path, payload: bytes) -> bool:
    try:
        if not path.is_file() or path.stat().st_size != len(payload):
            return False
        return path.read_bytes() == payload
    except OSError:
        return False


def _same_file(source: Path, destination: Path) -> bool:
    try:
        if not destination.is_file() or source.stat().st_size != destination.stat().st_size:
            return False
        with source.open("rb") as left, destination.open("rb") as right:
            while True:
                a = left.read(1 << 16)
                b = right.read(1 << 16)
                if a != b:
                    return False
                if not a:
                    return True
    except OSError:
        return False


def strm_payload(locator: str) -> bytes:
    """Exact bytes :func:`write_strm` produces for ``locator``."""

    return (locator.strip() + "\n").encode("utf-8")


def _atomic_write_text(destination: Path, text: str) -> None:
    from ..media.atomicfile import write_text as atomic_write_text

    atomic_write_text(destination, text)


def _artwork_sources(work: Work) -> dict[str, Path]:
    sources: dict[str, Path] = {}
    for item in work.artwork or []:
        raw = item.get("local_path") if isinstance(item, dict) else None
        if not isinstance(raw, str) or not raw:
            continue
        path = Path(raw)
        if not path.is_file():
            continue
        kind = str(item.get("kind", "thumb")).casefold()
        if kind == "sample":
            continue
        key = "fanart" if kind in {"fanart", "background", "backdrop"} else "poster"
        sources.setdefault(key, path)
    if sources:
        first = next(iter(sources.values()))
        sources.setdefault("poster", sources.get("fanart", first))
        sources.setdefault("fanart", sources.get("poster", first))
    return sources


def _sidecar(directory: Path) -> dict[str, object]:
    path = directory / SIDECAR_NAME
    if path.is_symlink():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        return payload if isinstance(payload, dict) else {}
    except (OSError, UnicodeError, ValueError):
        return {}


def _basename(name: object) -> bool:
    return isinstance(name, str) and bool(name) and name not in {".", "..", SIDECAR_NAME} and "/" not in name and "\\" not in name


def _digest_file(path: Path) -> str | None:
    try:
        if path.is_symlink() or not path.is_file():
            return None
        with path.open("rb") as stream:
            return hashlib.file_digest(stream, "sha256").hexdigest()
    except OSError:
        return None


def _can_publish(path: Path, previous: str | None) -> bool:
    return _digest_file(path) == previous if previous else not os.path.lexists(path)


def _digests(payload: dict[str, object], key: str) -> dict[str, str]:
    raw = payload.get(key)
    return {name: value for name, value in raw.items() if _basename(name)
            and isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)} if isinstance(raw, dict) else {}


def _owned(directory: Path, payload: dict[str, object]) -> dict[str, str]:
    return {name: value for name, value in _digests(payload, "managed").items()
            if _digest_file(directory / name) == value}


def _publish_sidecar(directory: Path, payload: dict[str, object]) -> bool:
    global _export_revision
    target = directory / SIDECAR_NAME
    if target.is_symlink():
        raise ValueError("STRM 清单路径是符号链接")
    output = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    if _same_bytes(target, output.encode("utf-8")):
        return False
    _atomic_write_text(target, output)
    _export_revision += 1
    return True


def export_revision() -> int:
    return _export_revision


def export_source_scope(source: SourceFingerprint) -> str:
    return hashlib.sha256(json.dumps([source.backend, source.account_id, source.directory_id]).encode()).hexdigest()


def read_sidecar_scope(directory: Path) -> str | None:
    value = _sidecar(directory).get("source_scope")
    return value if isinstance(value, str) else None


def read_sidecar(directory: Path) -> list[StrmEntry]:
    payload = _sidecar(directory)
    raw = payload.get("entries")
    entries: list[StrmEntry] = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict) or not _basename(item.get("name")) or not item.get("file_id"):
            continue
        entries.append(
            StrmEntry(
                name=str(item["name"]),
                file_id=str(item["file_id"]),
                pick_code=str(item["pick_code"]) if item.get("pick_code") else None,
                remote_path=str(item["remote_path"]) if item.get("remote_path") else None,
                sign=str(item["sign"]) if item.get("sign") else None,
            )
        )
    return entries


def write_sidecar(
    directory: Path, code: str, entries: Sequence[StrmEntry], *, work_id: str | None,
    managed: dict[str, str] | None = None, nfo_fields: dict[str, str] | None = None,
    source_scope: str | None = None,
) -> bool:
    """Write the sidecar; returns False (and leaves the file alone) when unchanged."""

    payload: dict[str, object] = {
        "version": 2 if managed is not None else 1,
        "code": code,
        "work_id": work_id,
        "entries": [
            {
                "name": entry.name,
                "file_id": entry.file_id,
                "pick_code": entry.pick_code,
                "remote_path": entry.remote_path,
                **({"backend": "openlist", "sign": entry.sign} if entry.is_openlist else {}),
            }
            for entry in entries
        ],
    }
    if managed is not None:
        payload.update(managed=managed, nfo_fields=nfo_fields or {})
    if source_scope:
        payload["source_scope"] = source_scope
    return _publish_sidecar(directory, payload)


def _write_planned(directory: Path, payload: dict[str, object], name: str, digest: str) -> None:
    pending = _digests(payload, "pending")
    pending[name] = digest
    payload["pending"] = pending
    _publish_sidecar(directory, payload)


def _select_output(directory: Path, name: str, allowed: dict[str, str], identity: str) -> Path:
    target = directory / name
    if name in allowed or not os.path.lexists(target):
        return target
    key = hashlib.sha256(identity.encode()).hexdigest()[:12]
    counter = 0
    while True:
        suffix = f"-{counter}" if counter else ""
        alternative = target.with_name(f"{target.stem} [{key}{suffix}]{target.suffix}")
        if alternative.name in allowed or not os.path.lexists(alternative):
            return alternative
        counter += 1


def _write_details(
    directory: Path, payload: dict[str, object], owned: dict[str, str],
    work: Work, identities: list[ExternalIdentity], result: ExportResult,
) -> dict[str, str]:
    local_artwork: dict[str, str] = {}
    for kind, source in _artwork_sources(work).items():
        extension = ".jpg" if source.suffix.casefold() in {".jpg", ".jpeg"} else source.suffix.casefold()
        target = directory / f"{kind}{extension}"
        if target.is_symlink():
            continue
        if target.name in owned or not target.exists():
            digest = _digest_file(source)
            if digest is None:
                continue
            if _digest_file(target) != digest:
                _write_planned(directory, payload, target.name, digest)
                if not _can_publish(target, owned.get(target.name)):
                    raise OSError("Artwork changed during export")
                _atomic_copy(source, target)
                result.changed_paths.append(target)
            if _digest_file(target) == digest:
                owned[target.name] = digest
        if target.is_file():
            result.artwork_paths.append(target)
            local_artwork[kind] = target.name
    nfo_path = directory / "movie.nfo"
    result.nfo_path = nfo_path
    baseline = _digests(payload, "nfo_fields")
    if nfo_path.is_symlink() or (nfo_path.exists() and not nfo_path.is_file()):
        return baseline
    content = build_nfo(work, identities, local_artwork=local_artwork or None)
    existed = nfo_path.is_file()
    previous = nfo_path.read_bytes() if existed else None
    # An untracked or malformed document remains untouched.
    if existed and nfo_path.name not in owned and not baseline:
        return baseline
    merged = merge_export_nfo(previous, content, baseline)
    if merged is None:
        return baseline
    output, fields = merged
    encoded = output.encode("utf-8")
    if encoded != previous:
        _write_planned(directory, payload, nfo_path.name, hashlib.sha256(encoded).hexdigest())
        if nfo_path.is_symlink() or (existed and nfo_path.read_bytes() != previous) or (not existed and os.path.lexists(nfo_path)):
            raise OSError("NFO changed during export")
        write_nfo(nfo_path, output)
        result.changed_paths.append(nfo_path)
    if fields:
        # A retained manual field makes the document ineligible for bulk deletion.
        if fields == nfo_export_fields(content) and (nfo_path.name in owned or not existed):
            owned[nfo_path.name] = hashlib.sha256(encoded).hexdigest()
        else:
            owned.pop(nfo_path.name, None)
    return fields


def export_work(
    *,
    settings: PanSettings,
    code: str,
    videos: Sequence[RemoteVideo],
    work: Work | None = None,
    identities: list[ExternalIdentity] | None = None,
    source_scope: str | None = None,
    complete: bool = True,
) -> ExportResult:
    """Write ``{root}/{studio}/{CODE}/``: artwork → movie.nfo → sidecar → ``.strm`` last.

    Layout is multi-level (never a flat dump under the export root). Files whose
    bytes are already identical are skipped; ``result.changed`` tells the caller
    whether anything on disk moved, so a no-op re-export can skip Emby notify.
    """

    require_strm_config(settings)
    root = settings.strm_output_root
    assert root  # guaranteed by require_strm_config
    entries = plan_strm_entries(code, videos)
    if not entries:
        raise ValueError("no video files to export")
    # Resolve every locator before touching disk so a bad config writes nothing.
    locators = [(entry, locator_for(settings, entry)) for entry in entries]
    relative = export_relative_dir(code, work, template=settings.strm_layout_template)
    directory = Path(root) / relative
    with _EXPORT_LOCK:
        if not _is_within(directory, Path(root)):
            raise ValueError("STRM 导出目录超出根路径")
        result = ExportResult(directory=directory, created=not directory.exists())
        directory.mkdir(parents=True, exist_ok=True)
        payload = _sidecar(directory)
        previous_scope = read_sidecar_scope(directory)
        if previous_scope and source_scope and previous_scope != source_scope:
            raise ValueError("目录属于其他网盘账号或目录")
        previous = read_sidecar(directory)
        owned = _owned(directory, payload)
        pending = _digests(payload, "pending")
        owned.update({name: digest for name, digest in pending.items()
                      if _digest_file(directory / name) == digest})
        # An old manifest can be adopted only when the published locator is reproducible.
        if payload.get("version") == 1:
            for entry in previous:
                try:
                    digest = hashlib.sha256(strm_payload(locator_for(settings, entry))).hexdigest()
                except ValueError:
                    continue
                if _digest_file(directory / entry.name) == digest:
                    owned[entry.name] = digest
        old_streams = {name: digest for name, digest in owned.items() if name.lower().endswith(".strm")}
        old_ids = {entry.name: entry.file_id for entry in previous}
        nfo_fields = _digests(payload, "nfo_fields")
        if work is not None:
            nfo_fields = _write_details(directory, payload, owned, work, identities or [], result)
        published: list[StrmEntry] = []
        previous_names = {entry.file_id: entry.name for entry in previous}
        for entry, locator in locators:
            if not complete and entry.file_id in previous_names:
                entry = replace(entry, name=previous_names[entry.file_id])
            writable = {name: digest for name, digest in owned.items()
                        if old_ids.get(name, entry.file_id) == entry.file_id}
            target = _select_output(directory, entry.name, writable, entry.file_id)
            digest = hashlib.sha256(strm_payload(locator)).hexdigest()
            current = _digest_file(target)
            if current != digest:
                _write_planned(directory, payload, target.name, digest)
                if not _can_publish(target, writable.get(target.name)):
                    raise OSError("STRM changed during export")
                write_strm(target, locator)
                result.changed_paths.append(target)
            owned[target.name] = digest
            result.strm_paths.append(target)
            published.append(replace(entry, name=target.name))
        if not complete:
            demonstrated = {entry.file_id for entry in published}
            published.extend(entry for entry in previous if entry.file_id not in demonstrated)
        keep = {entry.name for entry in published}
        for name, digest in old_streams.items():
            target = directory / name
            if complete and name not in keep and _digest_file(target) == digest:
                target.unlink()
                result.changed_paths.append(target)
                owned.pop(name, None)
        write_sidecar(directory, code, published, work_id=work.id if work is not None else None,
                      managed=owned, nfo_fields=nfo_fields, source_scope=source_scope or previous_scope)
        return result


def iter_export_dirs(root: Path) -> Iterable[Path]:
    if not root.is_dir():
        return
    for sidecar in sorted(root.rglob(SIDECAR_NAME)):
        yield sidecar.parent


def rewrite_strm_tree(root: Path, settings: PanSettings) -> RewriteResult:
    """Rewrite ``.strm`` bodies in place after token / public URL / mode changes.

    File ids come from the sidecar or, for relay links, from the old body itself,
    so no 115 calls and no re-export are needed.
    """

    require_strm_config(settings.model_copy(update={"strm_output_root": str(root)}))
    result = RewriteResult()
    if not root.is_dir():
        return result
    with _EXPORT_LOCK:
        for directory in iter_export_dirs(root):
            if not _is_within(directory, root):
                continue
            payload = _sidecar(directory)
            if payload.get("pending"):
                continue
            managed = _digests(payload, "managed")
            before = len(result.rewritten)
            for entry in read_sidecar(directory):
                result.scanned += 1
                strm = directory / entry.name
                current = _digest_file(strm)
                if current is None or managed.get(entry.name) != current:
                    result.skipped += 1
                    continue
                try:
                    locator = locator_for(settings, entry)
                except ValueError:
                    result.skipped += 1
                    continue
                digest = hashlib.sha256(strm_payload(locator)).hexdigest()
                if digest == current:
                    continue
                _write_planned(directory, payload, entry.name, digest)
                if _digest_file(strm) != current:
                    result.skipped += 1
                    continue
                write_strm(strm, locator)
                managed[entry.name] = digest
                result.rewritten.append(strm)
            if len(result.rewritten) > before or "pending" in payload:
                payload["managed"] = managed
                payload.pop("pending", None)
                _publish_sidecar(directory, payload)
    return result


def _move_export_dir(source: Path, target: Path) -> None:
    """Publish only verified generated files in the new layout."""
    import tempfile

    with _EXPORT_LOCK:
        payload = _sidecar(source)
        owned = _owned(source, payload)
        entries = read_sidecar(source)
        if not entries or not all(entry.name in owned for entry in entries) or payload.get("pending"):
            raise OSError("STRM 清单缺少迁移所需的文件归属")
        if target.exists():
            raise FileExistsError(target)
        target.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=f".{target.name}-", dir=target.parent) as temp:
            staged = Path(temp) / "export"
            staged.mkdir()
            for name, digest in owned.items():
                _atomic_copy(source / name, staged / name)
                if _digest_file(staged / name) != digest or _digest_file(source / name) != digest:
                    raise OSError("STRM 文件在迁移期间变化")
            payload["managed"] = owned
            if "movie.nfo" not in owned:
                payload.pop("nfo_fields", None)
            _publish_sidecar(staged, payload)
            if target.exists():
                raise FileExistsError(target)
            os.rename(staged, target)
        for name, digest in owned.items():
            if _digest_file(source / name) == digest:
                (source / name).unlink()
        (source / SIDECAR_NAME).unlink()
        with contextlib.suppress(OSError):
            source.rmdir()


def _prune_empty_parents(start: Path, root: Path) -> None:
    current = start
    while current != root and _is_within(current, root):
        try:
            current.rmdir()
        except OSError:
            return
        current = current.parent


def migrate_strm_tree(old_root: Path, new_root: Path, settings: PanSettings) -> MigrateResult:
    """Move every managed export folder (has a sidecar) from ``old_root`` to ``new_root``.

    Relative layout is kept. A folder whose target already holds an export is
    left in place and reported as a conflict (never overwritten). Afterwards
    every ``.strm`` under ``new_root`` is rewritten for the current settings, so
    the result equals a clean re-export without any 115 / OpenList calls.
    Unmanaged files in the old root are never touched.
    """

    # Refuse before moving anything: a half-configured target must not be built.
    require_strm_config(settings.model_copy(update={"strm_output_root": str(new_root)}))
    result = MigrateResult(old_root=old_root, new_root=new_root)
    new_root.mkdir(parents=True, exist_ok=True)
    same_root = old_root.resolve() == new_root.resolve() if old_root.exists() else False
    if old_root.is_dir() and not same_root:
        new_resolved = new_root.resolve()
        sources = [
            directory
            for directory in iter_export_dirs(old_root)
            # New root nested inside the old one: never re-move what is already there.
            if not (directory.resolve() == new_resolved or _is_within(directory, new_root))
        ]
        for source in sources:
            try:
                relative = source.resolve().relative_to(old_root.resolve())
            except ValueError:
                result.failed.append(source)
                continue
            if not relative.parts:
                # Sidecar directly in the root: the root itself is an export; skip.
                result.failed.append(source)
                continue
            target = new_root / relative
            if target.exists() or not _is_within(target, new_root):
                result.conflicts.append(source)
                continue
            try:
                _move_export_dir(source, target)
            except OSError:
                result.failed.append(source)
                continue
            result.moved.append((source, target))
            with contextlib.suppress(OSError):
                _prune_empty_parents(source.parent, old_root)
    result.rewrite = rewrite_strm_tree(new_root, settings)
    return result


def _is_within(child: Path, parent: Path) -> bool:
    try:
        child.resolve().relative_to(parent.resolve())
    except ValueError:
        return False
    return child.resolve() != parent.resolve()


async def reconcile_deleted(
    root: Path,
    exists: Callable[[str], Awaitable[bool | None]],
    *, source_scope: str | None = None,
) -> ReconcileResult:
    """Reconcile fully missing sources against the last successful publication."""
    result = ReconcileResult()
    for directory in list(iter_export_dirs(root)):
        if not _is_within(directory, root):
            continue
        with _EXPORT_LOCK:
            payload = _sidecar(directory)
            entries = read_sidecar(directory)
        if not entries:
            continue
        result.checked += 1
        if source_scope is not None and payload.get("source_scope") != source_scope:
            result.unknown += 1
            continue
        states = [await exists(entry.file_id) for entry in entries]
        with _EXPORT_LOCK:
            if any(state is None for state in states) or payload != _sidecar(directory) or payload.get("pending"):
                result.unknown += 1
                continue
            if any(states):
                result.kept += 1
                continue
            deleted = cleanup_generated_export_files(directory, str(payload.get("code", "")))
            if not deleted:
                result.kept += 1
                continue
            try:
                directory.rmdir()
            except OSError:
                result.orphan_strm_removed.extend(path for path in deleted if path.suffix == ".strm")
                result.kept += 1
            else:
                result.removed.append(directory)
                _prune_empty_parents(directory.parent, root)
    return result


def rematerialize_layout(
    root: Path,
    settings: PanSettings,
    *,
    resolve_work: Callable[[str, str | None], Work | None] | None = None,
    identities_for: Callable[[Work], Sequence[ExternalIdentity]] | None = None,
) -> RematerializeResult:
    """Move managed export folders so their relative path matches ``strm_layout_template``.

    Used when the layout template changes (e.g. flat ``{code}`` → ``{studio}/{code}``).
    Sidecar ``code`` / ``work_id`` drive the destination; optional callbacks supply
    ``Work`` / identities so missing ``movie.nfo`` and artwork can be regenerated.
    Conflicts (target already occupied) are left in place.
    """

    require_strm_config(settings.model_copy(update={"strm_output_root": str(root)}))
    result = RematerializeResult()
    if not root.is_dir():
        return result
    template = settings.strm_layout_template or DEFAULT_STRM_LAYOUT
    for directory in list(iter_export_dirs(root)):
        result.scanned += 1
        sidecar_path = directory / SIDECAR_NAME
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            result.failed.append(directory)
            continue
        if not isinstance(payload, dict):
            result.failed.append(directory)
            continue
        code = str(payload.get("code") or "").strip()
        if not code:
            # Fall back to folder name for older sidecars.
            code = directory.name
        work_id = payload.get("work_id")
        work_id_s = str(work_id) if isinstance(work_id, str) and work_id.strip() else None
        work = resolve_work(code, work_id_s) if resolve_work is not None else None
        try:
            relative = export_relative_dir(code, work, template=template)
        except ValueError:
            result.failed.append(directory)
            continue
        target = root / relative
        try:
            same = directory.resolve() == target.resolve()
        except OSError:
            same = False
        if not same:
            if target.exists() or not _is_within(target, root) or not _is_within(directory, root):
                result.conflicts.append(directory)
                continue
            try:
                _move_export_dir(directory, target)
            except OSError:
                result.failed.append(directory)
                continue
            result.moved.append((directory, target))
            with contextlib.suppress(OSError):
                _prune_empty_parents(directory.parent, root)
            directory = target
        else:
            result.skipped += 1
        if work is not None:
            with _EXPORT_LOCK:
                payload = _sidecar(directory)
                entries = read_sidecar(directory)
                owned = _owned(directory, payload)
                updates = ExportResult(directory)
                identities = list(identities_for(work)) if identities_for is not None else []
                fields = _write_details(directory, payload, owned, work, identities, updates)
                if updates.changed:
                    write_sidecar(directory, code, entries, work_id=work.id, managed=owned, nfo_fields=fields,
                                  source_scope=read_sidecar_scope(directory))
                result.nfo_written += sum(path.name == "movie.nfo" for path in updates.changed_paths)
                result.artwork_written += sum(path.name != "movie.nfo" for path in updates.changed_paths)
    return result



def find_export_dirs_for_code(root: Path, code: str) -> list[Path]:
    """Managed export folders whose sidecar (or folder name) matches ``code``."""

    from ..normalize_code import is_format_equivalent, normalize_code

    target = normalize_code(code) or code.strip()
    if not target or not root.is_dir():
        return []
    found: list[Path] = []
    for directory in iter_export_dirs(root):
        sidecar_path = directory / SIDECAR_NAME
        sidecar_code = ""
        try:
            payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                sidecar_code = str(payload.get("code") or "").strip()
        except (OSError, UnicodeError, ValueError):
            sidecar_code = ""
        candidate = sidecar_code or directory.name
        if is_format_equivalent(candidate, target) or (
            normalize_code(candidate) == normalize_code(target)
        ):
            found.append(directory)
    return found


_CD_STREAM_RE = re.compile(r"^(?P<stem>.+)-cd(?P<n>[1-9]\d*)\.strm$", re.IGNORECASE)
_ART_NAMES = frozenset(
    {
        "poster.jpg",
        "poster.jpeg",
        "poster.png",
        "poster.webp",
        "fanart.jpg",
        "fanart.jpeg",
        "fanart.png",
        "fanart.webp",
    }
)


def generated_export_names(code: str) -> frozenset[str]:
    """Writer-known basenames for one export folder stem (ideas-only, no GPL copy).

    Exact generated set: ``movie.nfo``, ``{stem}.nfo``, ``{stem}.strm``,
    ``{stem}-cdN.strm`` (N = positive int, no leading zero), poster/fanart image
    names the exporter writes, and the managed ``.shadow-strm.json`` sidecar.
    """

    stem = safe_stem(code)
    names = {
        "movie.nfo",
        f"{stem}.nfo",
        f"{stem}.strm",
        SIDECAR_NAME,
        *{name.casefold() for name in _ART_NAMES},
    }
    # cd parts are matched dynamically in cleanup; stem.strm / movie.nfo covered above.
    return frozenset(names)


def is_generated_export_name(filename: str, code: str) -> bool:
    """True when ``filename`` is a writer-known generated sidecar for ``code``."""

    name = Path(filename).name
    stem = safe_stem(code)
    folded = name.casefold()
    if folded in {n.casefold() for n in generated_export_names(code)}:
        return True
    if folded in _ART_NAMES:
        return True
    match = _CD_STREAM_RE.match(name)
    if match and safe_stem(match.group("stem")) == stem:
        return True
    return False


def cleanup_generated_export_files(directory: Path, code: str) -> list[Path]:
    """Remove files proven to be unchanged outputs of this export."""
    global _export_revision
    deleted: list[Path] = []
    with _EXPORT_LOCK:
        payload = _sidecar(directory)
        if payload.get("version") != 2 or payload.get("code") != code or payload.get("pending"):
            return deleted
        managed = _digests(payload, "managed")
        for name, digest in managed.items():
            target = directory / name
            if _digest_file(target) == digest:
                target.unlink()
                deleted.append(target)
        target = directory / SIDECAR_NAME
        if not target.is_symlink() and target.is_file():
            target.unlink()
            deleted.append(target)
        if deleted:
            _export_revision += 1
    return deleted


def remove_export_dirs_for_code(root: Path, code: str) -> list[Path]:
    """Remove generated sidecars for ``code``; drop empty export dirs under root.

    Narrow cleanup (generated names only) so user custom files in the old folder
    are preserved. Directories that still hold custom files stay on disk; empty
    parents under the export root are pruned.
    """

    touched: list[Path] = []
    for directory in find_export_dirs_for_code(root, code):
        if not _is_within(directory, root):
            continue
        deleted = cleanup_generated_export_files(directory, str(_sidecar(directory).get("code", "")))
        touched.extend(deleted)
        # Remove the folder only when nothing user-owned remains.
        try:
            remaining = [p for p in directory.iterdir()]
        except OSError:
            remaining = ["?"]
        if not remaining:
            with contextlib.suppress(OSError):
                directory.rmdir()
                touched.append(directory)
            with contextlib.suppress(OSError):
                parent = directory.parent
                if parent != root and _is_within(parent, root) and parent.is_dir() and not any(parent.iterdir()):
                    parent.rmdir()
    return touched


def map_to_emby_path(local: Path, settings: PanSettings) -> str:
    """Translate a local export path into the path Emby sees (container mount)."""

    root = settings.strm_output_root
    emby_root = settings.strm_emby_root
    if not root or not emby_root:
        return str(local)
    try:
        relative = local.resolve().relative_to(Path(root).resolve())
    except ValueError:
        return str(local)
    base = emby_root.rstrip("/\\")
    if not relative.parts:
        return base
    separator = "\\" if "\\" in base and "/" not in base else "/"
    return base + separator + separator.join(relative.parts)
