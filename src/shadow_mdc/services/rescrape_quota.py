"""Per-provider daily budget for forced rescrapes (``POST /api/works/{id}/refresh``).

Counters reset at local midnight (``timezone``) and persist to a small JSON file
so restarts do not hand out a fresh budget.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterable
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class RescrapeQuota:
    def __init__(
        self,
        daily_limit: int,
        *,
        path: Path | None = None,
        timezone: str = "Asia/Shanghai",
        now: Callable[[], datetime] | None = None,
    ):
        self.daily_limit = max(0, int(daily_limit))
        self._path = Path(path) if path is not None else None
        self._tz = ZoneInfo(timezone)
        self._now = now or (lambda: datetime.now(self._tz))
        self._lock = threading.Lock()
        self._day: str = ""
        self._counts: dict[str, int] = {}
        self._load()

    def _today(self) -> date:
        return self._now().astimezone(self._tz).date()

    def _load(self) -> None:
        if self._path is None or not self._path.is_file():
            return
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
            self._day = str(data.get("day", ""))
            self._counts = {str(k): int(v) for k, v in dict(data.get("counts", {})).items()}
        except (OSError, ValueError, TypeError):
            self._day, self._counts = "", {}

    def _save(self) -> None:
        if self._path is None:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(self._path.suffix + ".tmp")
        tmp.write_text(json.dumps({"day": self._day, "counts": self._counts}), encoding="utf-8")
        tmp.replace(self._path)

    def _roll(self) -> None:
        today = self._today().isoformat()
        if self._day != today:
            self._day, self._counts = today, {}

    def remaining(self, provider_id: str) -> int:
        with self._lock:
            self._roll()
            return max(0, self.daily_limit - self._counts.get(provider_id, 0))

    def consume(self, provider_ids: Iterable[str]) -> tuple[list[str], list[str]]:
        """Charge one unit per provider with budget left -> (allowed, exhausted)."""

        allowed: list[str] = []
        exhausted: list[str] = []
        with self._lock:
            self._roll()
            for provider_id in provider_ids:
                if self._counts.get(provider_id, 0) >= self.daily_limit:
                    exhausted.append(provider_id)
                    continue
                self._counts[provider_id] = self._counts.get(provider_id, 0) + 1
                allowed.append(provider_id)
            if allowed:
                self._save()
        return allowed, exhausted

    def seconds_until_reset(self) -> int:
        now = self._now().astimezone(self._tz)
        tomorrow = datetime.combine(now.date() + timedelta(days=1), datetime.min.time(), self._tz)
        return max(1, int((tomorrow - now).total_seconds()))
