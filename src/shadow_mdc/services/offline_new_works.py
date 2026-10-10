"""Compatibility facade for daily-chart/hot offline enqueue.

Canonical implementation: ``seed_auto_offline`` (used by seed scripts and
``scripts/enqueue_seed_offline.py``). This module re-exports the same helpers
so routines / docs that name ``offline_new_works`` keep working.
"""

from __future__ import annotations

from .seed_auto_offline import (
    DEFAULT_PACE_SECONDS,
    SeedOfflineStats,
    collect_new_seed_work_ids,
    enqueue_seeded_offline,
    resolve_work_ids_by_codes,
    resolve_work_ids_for_day,
    work_ids_from_run_logs,
)

# Friendly aliases matching the original task naming.
OfflineNewWorksResult = SeedOfflineStats
offline_works = enqueue_seeded_offline

__all__ = (
    "DEFAULT_PACE_SECONDS",
    "OfflineNewWorksResult",
    "SeedOfflineStats",
    "collect_new_seed_work_ids",
    "enqueue_seeded_offline",
    "offline_works",
    "resolve_work_ids_by_codes",
    "resolve_work_ids_for_day",
    "work_ids_from_run_logs",
)
