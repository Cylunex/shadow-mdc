#!/usr/bin/env python3
"""Alias for ``enqueue_seed_offline.py`` (daily chart/hot → OpenList 115 offline).

Preferred entry points:
* ``seed_daily_chart.py`` / ``seed_daily_hot.py`` ``--auto-offline`` (default on)
* ``sync_catalog_to_nas.sh`` runs ``enqueue_seed_offline.py --today`` on NAS
* This script: same CLI as ``enqueue_seed_offline.py`` for routines that call
  ``offline_new_works``

Examples::

    PYTHONPATH=src .venv/bin/python scripts/offline_new_works.py --today --dry-run
    PYTHONPATH=src .venv/bin/python scripts/offline_new_works.py --code SNOS-380 --pace 2
"""

from __future__ import annotations

import runpy
from pathlib import Path

if __name__ == "__main__":
    runpy.run_path(str(Path(__file__).with_name("enqueue_seed_offline.py")), run_name="__main__")
