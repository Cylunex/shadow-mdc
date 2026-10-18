#!/usr/bin/env python3
"""NAS weekly r18 dump job: stop API → refresh → enrich → start API.

Mirrors the former Grok Bot routine. Prefer invoking via
``scripts/nas/r18_dump_weekly.sh`` from crontab::

    CRON_TZ=Asia/Shanghai
    1 10 * * 3 /data/project/shadow-mdc/current/scripts/nas/r18_dump_weekly.sh

Requires ``SHADOW_MDC_R18_DUMP_REFRESH_ENABLED=true`` (or ``--force``).
Never commits dump files or the SQLite DBs to git.
"""

from __future__ import annotations

import argparse
import json
import logging
import subprocess
import sys
import time
from pathlib import Path
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))

from shadow_mdc.config import Settings  # noqa: E402
from shadow_mdc.db.repository import Database  # noqa: E402
from shadow_mdc.services.r18_dump_download import R18DumpDownloadError  # noqa: E402
from shadow_mdc.services.r18_dump_refresh import (  # noqa: E402
    assert_enrich_did_not_grow_catalog,
    count_jav_catalog_gaps_from_database,
    enrich_existing_jav_from_dump,
    refresh_r18_dump,
    resolve_proxy,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
logger = logging.getLogger("r18_dump_weekly")


def _supervisorctl(*args: str) -> None:
    cmd = ["supervisorctl", *args]
    logger.info("exec %s", " ".join(cmd))
    subprocess.run(cmd, check=True)


def _wait_health(url: str, *, timeout_seconds: float = 90.0) -> None:
    deadline = time.monotonic() + timeout_seconds
    last_error = "not attempted"
    while time.monotonic() < deadline:
        try:
            with urlopen(url, timeout=5) as response:
                if 200 <= getattr(response, "status", 200) < 300:
                    logger.info("health ok %s", url)
                    return
                last_error = f"HTTP {getattr(response, 'status', '?')}"
        except (URLError, OSError, TimeoutError) as exc:
            last_error = str(exc)
        time.sleep(1)
    raise RuntimeError(f"health check failed within {timeout_seconds:.0f}s ({url}): {last_error}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, default=None)
    parser.add_argument("--database-url", default=None)
    parser.add_argument("--proxy", default=None, help="HTTPS proxy (default: Settings.proxy_url)")
    parser.add_argument("--service", default=None, help="supervisor program name")
    parser.add_argument("--health-url", default=None)
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run even when SHADOW_MDC_R18_DUMP_REFRESH_ENABLED is false",
    )
    parser.add_argument(
        "--skip-service-control",
        action="store_true",
        help="Do not supervisorctl stop/start (local dry exercise)",
    )
    parser.add_argument(
        "--force-import",
        action="store_true",
        help="Re-import SQLite even when the dated dump file is unchanged",
    )
    parser.add_argument("--dry-run-enrich", action="store_true")
    arguments = parser.parse_args(argv)

    updates: dict[str, object] = {}
    if arguments.data_dir is not None:
        updates["data_dir"] = arguments.data_dir
    if arguments.database_url is not None:
        updates["database_url"] = arguments.database_url
    settings = Settings()
    if updates:
        settings = settings.model_copy(update=updates)
    settings.ensure_directories()

    if not settings.r18_dump_refresh_enabled and not arguments.force:
        logger.warning(
            "r18 dump weekly refresh disabled "
            "(set SHADOW_MDC_R18_DUMP_REFRESH_ENABLED=true or pass --force); exiting"
        )
        return 0

    service = arguments.service or settings.r18_dump_refresh_service
    health_url = arguments.health_url or settings.r18_dump_refresh_health_url
    proxy = resolve_proxy(settings, arguments.proxy)

    logger.info(
        "r18 dump weekly start cron=%s tz=%s data_dir=%s proxy=%s service=%s",
        settings.r18_dump_refresh_cron,
        settings.r18_dump_refresh_timezone,
        settings.data_dir,
        "set" if proxy else "none",
        service,
    )

    database = Database(settings.database_url)
    database.initialize()
    before = count_jav_catalog_gaps_from_database(database)
    logger.info("catalog before %s", json.dumps(before.as_dict(), ensure_ascii=False))

    stopped = False
    try:
        if not arguments.skip_service_control:
            _supervisorctl("stop", service)
            stopped = True

        try:
            outcome = refresh_r18_dump(
                settings,
                proxy=proxy,
                force_import=arguments.force_import,
            )
        except R18DumpDownloadError as exc:
            logger.error("r18 dump weekly download failed: %s", exc)
            return 2

        logger.info(
            "refresh done unchanged=%s imported=%s dump=%s",
            outcome.download.unchanged,
            outcome.imported,
            outcome.dump_path,
        )

        should_enrich = (not outcome.download.unchanged) or outcome.imported or arguments.force_import
        if should_enrich:
            enrich = enrich_existing_jav_from_dump(
                settings,
                sqlite_path=outcome.sqlite_path,
                dry_run=arguments.dry_run_enrich,
            )
            logger.info(
                "enrich done candidates=%s matched=%s updated=%s skipped=%s",
                enrich.candidates,
                enrich.matched,
                enrich.updated,
                enrich.skipped,
            )
        else:
            logger.info("dump unchanged and sqlite present; skipping enrich")

        if not arguments.skip_service_control:
            _supervisorctl("start", service)
            stopped = False
            _wait_health(health_url, timeout_seconds=90.0)

        after = count_jav_catalog_gaps_from_database(database)
        logger.info("catalog after %s", json.dumps(after.as_dict(), ensure_ascii=False))
        assert_enrich_did_not_grow_catalog(before, after)
        logger.info("r18 dump weekly success")
        return 0
    except Exception:
        logger.exception("r18 dump weekly failed")
        return 1
    finally:
        if stopped and not arguments.skip_service_control:
            logger.warning("ensuring API restart after failure")
            try:
                _supervisorctl("start", service)
            except Exception:
                logger.exception("failed to restart %s", service)


if __name__ == "__main__":
    raise SystemExit(main())
