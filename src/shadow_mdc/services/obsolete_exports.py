"""Pending Emby-export cleanup after scan rebinds files away from a 番号.

When files move to a different catalogue identity and the old code no longer
exists in the library, keep a durable cross-chunk pending list, remove the
matching STRM/NFO export directories, and notify Emby (retry-safe). Miyabi-
inspired behaviour; original GPL scan code is not copied.
"""

from __future__ import annotations

import json
import logging
import os
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .pan import PanSettings
from .strm_export import map_to_emby_path, remove_export_dirs_for_code

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class ObsoleteExportDrainResult:
    attempted: list[str] = field(default_factory=list)
    removed: list[Path] = field(default_factory=list)
    skipped_still_present: list[str] = field(default_factory=list)
    remaining: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class ObsoleteExportQueue:
    """Durable pending-delete list of catalogue codes whose exports should go."""

    def __init__(self, state_path: Path | None) -> None:
        self._state_path = state_path
        self._codes: list[str] = []
        self._load()

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(self._codes)

    def mark(self, codes: Iterable[str]) -> int:
        """Append unique non-empty codes; returns how many were newly queued."""

        added = 0
        known = {item.casefold() for item in self._codes}
        for raw in codes:
            code = str(raw or "").strip()
            if not code:
                continue
            key = code.casefold()
            if key in known:
                continue
            self._codes.append(code)
            known.add(key)
            added += 1
        if added:
            self._save()
        return added

    def clear(self) -> None:
        self._codes = []
        self._save()

    def drain(
        self,
        *,
        settings: PanSettings,
        code_exists: Callable[[str], bool],
        notify: Callable[[Sequence[str], str], None] | None = None,
    ) -> ObsoleteExportDrainResult:
        """Delete export dirs for queued codes that no longer exist in the library.

        Codes that still have a Work stay queued until absent (or are dropped when
        the caller stops marking them). Removal/notify failures keep the code for
        the next drain attempt.
        """

        result = ObsoleteExportDrainResult()
        root = (settings.strm_output_root or "").strip()
        pending = list(self._codes)
        kept: list[str] = []
        for code in pending:
            result.attempted.append(code)
            try:
                if code_exists(code):
                    result.skipped_still_present.append(code)
                    continue
            except Exception as exc:
                result.errors.append(f"{code}: exists-check failed: {exc}")
                kept.append(code)
                continue
            if not root:
                kept.append(code)
                continue
            try:
                removed = remove_export_dirs_for_code(Path(root), code)
            except OSError as exc:
                result.errors.append(f"{code}: remove failed: {exc}")
                kept.append(code)
                continue
            result.removed.extend(removed)
            if notify is not None and removed:
                try:
                    paths = [map_to_emby_path(path, settings) for path in removed]
                    notify(paths, "Deleted")
                except Exception as exc:
                    result.errors.append(f"{code}: notify failed: {exc}")
                    kept.append(code)
                    continue
            # Success (including "nothing on disk"): drop from queue.
        self._codes = kept
        result.remaining = list(kept)
        self._save()
        return result

    def _load(self) -> None:
        if self._state_path is None or not self._state_path.is_file():
            return
        try:
            payload = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, ValueError):
            logger.warning("obsolete export queue unreadable; starting empty")
            return
        if not isinstance(payload, dict):
            return
        codes: list[str] = []
        seen: set[str] = set()
        for item in payload.get("codes") or []:
            if not isinstance(item, str):
                continue
            code = item.strip()
            key = code.casefold()
            if not code or key in seen:
                continue
            codes.append(code)
            seen.add(key)
        self._codes = codes
        if codes:
            logger.info("obsolete exports: resumed %d pending code(s)", len(codes))

    def _save(self) -> None:
        if self._state_path is None:
            return
        payload = {"version": 1, "codes": list(self._codes)}
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            temporary = self._state_path.with_name(f".{self._state_path.name}.{os.getpid()}.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            temporary.replace(self._state_path)
        except OSError:
            logger.warning("obsolete export queue could not be saved", exc_info=True)
