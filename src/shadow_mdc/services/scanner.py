from pathlib import Path

from pydantic import BaseModel, ConfigDict

from ..db.models import Library
from ..db.repository import Repository
from ..identity import build_identity_hints
from ..media.oshash import compute_oshash
from ..media.probe import probe_duration

MEDIA_EXTENSIONS = frozenset({".mp4", ".mkv", ".avi", ".mov", ".wmv", ".m4v", ".ts", ".webm"})


class ScanResult(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    discovered: int
    updated: int
    skipped: int
    errors: tuple[str, ...]


class Scanner:
    def __init__(self, repository: Repository):
        self._repository = repository

    def scan(self, library: Library) -> ScanResult:
        root = Path(library.root_path).resolve()
        if not root.is_dir():
            raise ValueError(f"library root is not a directory: {root}")
        iterator = root.rglob("*") if library.recursive else root.glob("*")
        discovered = 0
        updated = 0
        skipped = 0
        errors: list[str] = []
        for path in iterator:
            if not path.is_file() or path.suffix.casefold() not in MEDIA_EXTENSIONS:
                skipped += 1
                continue
            try:
                stat = path.stat()
                duration = probe_duration(path)
                oshash = compute_oshash(path)
                fingerprints = {"oshash": oshash} if oshash else {}
                hints = build_identity_hints(path, fingerprints=fingerprints, duration_seconds=duration)
                _, created = self._repository.upsert_asset(
                    library_id=library.id,
                    path=str(path),
                    size=stat.st_size,
                    duration_seconds=duration,
                    oshash=oshash,
                    hints=hints,
                )
                discovered += int(created)
                updated += int(not created)
            except (OSError, ValueError) as exc:
                errors.append(f"{path}: {exc}")
        return ScanResult(discovered=discovered, updated=updated, skipped=skipped, errors=tuple(errors))
