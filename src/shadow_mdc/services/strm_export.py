"""115 → local Emby-style STRM export.

Re-implemented from the *ideas* noted in docs/references (Miyabi GPL-3: ideas only,
no code; openStrm MIT patterns for the 302 gateway):

* Export order per title: poster/fanart → NFO → ``.strm`` last, each written to a
  temp file and renamed, so Emby realtime monitors never see a half-built folder.
* ``.strm`` body is either our relay URL ``{public}/api/strm/play/{file_id}[?token=]``
  (302 → 115 direct link) or the legacy OpenList ``{prefix}/{remote path}`` form.
* A small sidecar (``.shadow-strm.json``) records file ids so token / public URL
  rotation can rewrite ``.strm`` bodies in place and delete reconciliation knows
  which 115 files back each folder.
* OpenList backend: entries are keyed by the absolute OpenList path (``file_id``
  starts with ``/``) and the body is ``{openlist}/d{path}[?sign=…]``, or the relay
  ``{public}/api/strm/openlist{path}[?token=]`` which 302s to that ``/d`` link.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import tempfile
from collections.abc import Awaitable, Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import quote, urlencode, urlsplit

from ..db.models import ExternalIdentity, Work
from ..media.nfo import build_nfo, write_nfo
from ..media.strm import write_strm
from .openlist import build_openlist_d_url
from .pan import PanSettings, RemoteVideo

SIDECAR_NAME = ".shadow-strm.json"
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


@dataclass(slots=True)
class ExportResult:
    directory: Path
    strm_paths: list[Path] = field(default_factory=list)
    nfo_path: Path | None = None
    artwork_paths: list[Path] = field(default_factory=list)


@dataclass(slots=True)
class RewriteResult:
    scanned: int = 0
    rewritten: list[Path] = field(default_factory=list)
    skipped: int = 0


@dataclass(slots=True)
class ReconcileResult:
    checked: int = 0
    removed: list[Path] = field(default_factory=list)
    kept: int = 0
    unknown: int = 0


def safe_stem(value: str) -> str:
    cleaned = _UNSAFE.sub("_", value).strip().strip(".")
    return cleaned[:120] or "untitled"


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
    match = _PART_RE.search(Path(name).stem)
    return int(match.group(1)) if match else None


def plan_strm_entries(code: str, videos: Sequence[RemoteVideo]) -> list[StrmEntry]:
    """Name ``{code}.strm`` for one file or ``{code}-cdN.strm`` for multi-part works.

    Tiny extras (samples/trailers) are dropped when a clearly larger main file exists.
    """

    stem = safe_stem(code)
    items = list(videos)
    sized = [item.size for item in items if item.size]
    if len(items) > 1 and sized:
        largest = max(sized)
        items = [item for item in items if not item.size or item.size >= largest * 0.2]
    if not items:
        return []
    if len(items) == 1:
        only = items[0]
        return [
            StrmEntry(
                f"{stem}.strm", only.file_id, only.pick_code, only.relative_path or only.name, only.sign
            )
        ]

    def sort_key(item: RemoteVideo) -> tuple[int, str]:
        part = detect_part_number(item.name)
        return (part if part is not None else 999, item.name.casefold())

    ordered = sorted(items, key=sort_key)
    return [
        StrmEntry(
            f"{stem}-cd{index}.strm",
            item.file_id,
            item.pick_code,
            item.relative_path or item.name,
            item.sign,
        )
        for index, item in enumerate(ordered, start=1)
    ]


def _atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}", suffix=".tmp", dir=destination.parent
    )
    os.close(descriptor)
    try:
        shutil.copyfile(source, temporary)
        os.replace(temporary, destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


def _atomic_write_text(destination: Path, text: str) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix=f".{destination.name}", suffix=".tmp", dir=destination.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        os.replace(temporary, destination)
    except Exception:
        Path(temporary).unlink(missing_ok=True)
        raise


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


def read_sidecar(directory: Path) -> list[StrmEntry]:
    path = directory / SIDECAR_NAME
    if not path.is_file():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return []
    entries: list[StrmEntry] = []
    for item in payload.get("entries", []) if isinstance(payload, dict) else []:
        if not isinstance(item, dict) or not item.get("name") or not item.get("file_id"):
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


def write_sidecar(directory: Path, code: str, entries: Sequence[StrmEntry], *, work_id: str | None) -> None:
    payload = {
        "version": 1,
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
    _atomic_write_text(directory / SIDECAR_NAME, json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def export_work(
    *,
    settings: PanSettings,
    code: str,
    videos: Sequence[RemoteVideo],
    work: Work | None = None,
    identities: list[ExternalIdentity] | None = None,
) -> ExportResult:
    """Write ``{root}/{code}/``: artwork → NFO → sidecar → ``.strm`` last (all atomic)."""

    root = settings.strm_output_root
    if not root:
        raise ValueError("strm_output_root is not configured")
    entries = plan_strm_entries(code, videos)
    if not entries:
        raise ValueError("no video files to export")
    # Resolve every locator before touching disk so a bad config writes nothing.
    locators = [(entry, locator_for(settings, entry)) for entry in entries]
    stem = safe_stem(code)
    directory = Path(root) / stem
    result = ExportResult(directory=directory)
    directory.mkdir(parents=True, exist_ok=True)

    # 1) poster / fanart
    if work is not None:
        for kind, source in _artwork_sources(work).items():
            extension = ".jpg" if source.suffix.casefold() in {".jpg", ".jpeg"} else source.suffix.casefold()
            target = directory / f"{kind}{extension}"
            _atomic_copy(source, target)
            result.artwork_paths.append(target)
    # 2) NFO
    if work is not None:
        nfo_path = directory / f"{stem}.nfo"
        write_nfo(nfo_path, build_nfo(work, identities or []))
        result.nfo_path = nfo_path
    # 3) sidecar (needed for later rewrite / reconcile)
    write_sidecar(directory, code, entries, work_id=work.id if work is not None else None)
    # 4) .strm last
    keep = {entry.name for entry in entries}
    for entry, locator in locators:
        result.strm_paths.append(write_strm(directory / entry.name, locator))
    # Drop stale .strm from a previous layout (e.g. single → multi-part).
    for stale in directory.glob("*.strm"):
        if stale.name not in keep:
            stale.unlink(missing_ok=True)
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

    result = RewriteResult()
    if not root.is_dir():
        return result
    for strm in sorted(root.rglob("*.strm")):
        result.scanned += 1
        sidecar = {entry.name: entry for entry in read_sidecar(strm.parent)}
        try:
            current = strm.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeError):
            result.skipped += 1
            continue
        entry = sidecar.get(strm.name)
        if entry is None:
            file_id = parse_relay_file_id(current)
            if file_id is None:
                result.skipped += 1
                continue
            entry = StrmEntry(strm.name, file_id)
        if settings.strm_mode != "relay" and not entry.remote_path:
            # OpenList mode needs a remote path we do not know; leave untouched.
            result.skipped += 1
            continue
        try:
            locator = locator_for(settings, entry)
        except ValueError:
            result.skipped += 1
            continue
        if locator == current:
            continue
        write_strm(strm, locator)
        result.rewritten.append(strm)
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
) -> ReconcileResult:
    """Remove export folders whose 115 / OpenList source files are all gone.

    ``exists(file_id)`` (115 id, or OpenList path for ``/``-prefixed ids) returns
    True/False, or None when unknown (network error,
    rate limit). Folders are only removed when *every* entry is definitively gone.
    """

    result = ReconcileResult()
    for directory in list(iter_export_dirs(root)):
        entries = read_sidecar(directory)
        if not entries:
            continue
        result.checked += 1
        states = [await exists(entry.file_id) for entry in entries]
        if any(state is None for state in states):
            result.unknown += 1
            continue
        if any(states):
            result.kept += 1
            continue
        if not _is_within(directory, root):
            result.unknown += 1
            continue
        shutil.rmtree(directory, ignore_errors=True)
        with contextlib.suppress(OSError):
            # Tidy an empty parent (e.g. grouped layouts) but never the root itself.
            parent = directory.parent
            if parent != root and _is_within(parent, root) and not any(parent.iterdir()):
                parent.rmdir()
        result.removed.append(directory)
    return result


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
