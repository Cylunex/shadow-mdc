"""Post-sync auto-offline selects bundle works by code, passes proxy; intakes default on."""

from __future__ import annotations

import json
import sqlite3
from datetime import date
from pathlib import Path

import pytest

from shadow_mdc.api_models import DiscoverSeedRequest
from shadow_mdc.db.repository import Database, Repository
from shadow_mdc.domain import ProviderRecord
from shadow_mdc.enums import ContentFamily, MediaCategory
from shadow_mdc.services.seed_auto_offline import (
    SeedOfflineStats,
    auto_offline_new_works,
    resolve_work_ids_created_on,
    work_codes_from_bundle,
)

ROOT = Path(__file__).resolve().parents[1]


def _bundle(tmp_path: Path, rows: list[tuple[str, str]], incremental: list[str] | None) -> Path:
    bundle = tmp_path / "bundle"
    (bundle / "data").mkdir(parents=True)
    conn = sqlite3.connect(bundle / "data" / "shadow-mdc.db")
    conn.execute("CREATE TABLE works (id TEXT, primary_code TEXT)")
    conn.executemany("INSERT INTO works VALUES (?, ?)", rows)
    conn.commit()
    conn.close()
    manifest: dict[str, object] = {"mode": "incremental"}
    if incremental is not None:
        manifest["incremental"] = {"works": incremental}
    (bundle / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return bundle


def test_work_codes_from_bundle_uses_incremental_ids(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, [("a", "DVAJ-759"), ("b", "MKMP-763"), ("c", "OLD-001")], ["a", "b"])
    assert work_codes_from_bundle(bundle) == ["DVAJ-759", "MKMP-763"]


def test_work_codes_from_bundle_full_and_missing(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path, [("a", "X-1"), ("b", None)], None)  # type: ignore[list-item]
    assert work_codes_from_bundle(bundle) == ["X-1"]
    assert work_codes_from_bundle(tmp_path / "nope") == []


def test_resolve_work_ids_created_on(tmp_path: Path) -> None:
    database = Database(f"sqlite:///{tmp_path / 't.db'}")
    database.initialize()
    with database.session() as session:
        repo = Repository(session)
        work = repo.upsert_provider_record(
            ProviderRecord(
                provider="javdb",
                external_id="x1",
                title="T",
                code="SSIS-001",
                family=ContentFamily.JAV,
                category=MediaCategory.JAPAN,
            ),
            overwrite=True,
        )
        work_id = work.id
    with database.session() as session:
        ids = resolve_work_ids_created_on(Repository(session), day=date.today())
        assert work_id in ids
        assert resolve_work_ids_created_on(Repository(session), day=date(2000, 1, 1)) == []


def test_stats_record_per_work() -> None:
    stats = SeedOfflineStats()
    stats.record("w", "ABC-1", "submitted", "hash")
    assert stats.as_dict()["results"] == [
        {"work_id": "w", "code": "ABC-1", "outcome": "submitted", "reason": "hash"}
    ]


@pytest.mark.asyncio
async def test_auto_offline_new_works_noop_without_ids() -> None:
    assert await auto_offline_new_works(settings=object(), database=None, discover=None, work_ids=["dry-run"]) is None  # type: ignore[arg-type]


def test_sync_script_passes_proxy_and_bundle() -> None:
    text = (ROOT / "scripts" / "sync_catalog_to_nas.sh").read_text(encoding="utf-8")
    assert "SHADOW_MDC_PROXY_URL='$NAS_PROXY_URL'" in text
    assert "enqueue_seed_offline.py --bundle '$REMOTE_BUNDLE' --today" in text
    assert "offline_rc" in text
    assert "|| true" not in text.split("auto-offline every work")[-1].split("advance export baseline")[0]


@pytest.mark.parametrize("script", ["seed_codes.py", "seed_weekly_vr.py", "seed_daily_hot.py", "seed_daily_chart.py"])
def test_intake_scripts_auto_offline_default_on(script: str) -> None:
    text = (ROOT / "scripts" / script).read_text(encoding="utf-8")
    assert '"--auto-offline"' in text and "default=True" in text and '"--no-auto-offline"' in text


def test_discover_seed_request_auto_offline_default() -> None:
    assert DiscoverSeedRequest(provider="javdb", code="X-1").auto_offline is True
