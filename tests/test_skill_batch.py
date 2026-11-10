"""Batch behavior: frozen selections, receipts, interrupted writes and explicit retry."""

import json
from pathlib import Path

import pytest
from pydantic import JsonValue

from shadow_mdc.skill_batch import prepare_batch, run_batch


def test_receipt_resumes_without_replaying_successes_or_silent_failure_retry(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.json"
    receipt = tmp_path / "receipt.json"
    manifest.write_text(
        json.dumps({"calls": [{"arguments": {"code": code}} for code in ("A-001", "B-002", "C-003")]})
    )
    called: list[str] = []

    def call(name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        code = str(arguments["code"])
        called.append(code)
        return {"ok": code != "B-002", "result": {"outcome": "created" if code == "A-001" else "skipped"}}

    result = run_batch(manifest, receipt, call, pace_seconds=0)
    assert result["counts"] == {"created": 1, "failed": 1, "skipped": 1}
    assert receipt.stat().st_mode & 0o077 == 0
    assert run_batch(manifest, receipt, call, pace_seconds=0)["ok"] is False
    assert called == ["A-001", "B-002", "C-003"]
    run_batch(manifest, receipt, call, retry_failed=True, pace_seconds=0)
    assert called == ["A-001", "B-002", "C-003", "B-002"]
    manifest.write_text('{"calls":[{"arguments":{"code":"D-004"}}]}')
    with pytest.raises(ValueError, match="manifest changed"):
        run_batch(manifest, receipt, call, pace_seconds=0)
    assert len(called) == 4


def test_unknown_result_stops_and_requires_reconciliation_before_retry(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.json"
    receipt = tmp_path / "receipt.json"
    manifest.write_text('{"calls":[{"arguments":{"code":"A-001"}},{"arguments":{"code":"B-002"}}]}')
    attempts: list[str] = []

    def disconnected(name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        attempts.append(str(arguments["code"]))
        raise TimeoutError

    with pytest.raises(TimeoutError):
        run_batch(manifest, receipt, disconnected, pace_seconds=0)
    result = run_batch(manifest, receipt, disconnected, pace_seconds=0)
    assert result["counts"] == {"unknown": 1, "pending": 1}
    assert attempts == ["A-001"]

    def reconciled(name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        return {"ok": True, "result": {"outcome": "skipped"}}

    assert run_batch(manifest, receipt, reconciled, retry_unknown=True, pace_seconds=0)["ok"] is True


def test_prepare_freezes_ten_thousand_ids_before_mutation(tmp_path: Path) -> None:
    seen_queries: list[dict[str, JsonValue]] = []

    def query(name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        assert name == "works.query" and arguments["languages"] == ["languages:unknown"]
        seen_queries.append(arguments)
        offset = int(str(arguments.get("cursor", 0)))
        return {
            "ok": True,
            "result": {
                "items": [{"id": f"work-{i}"} for i in range(offset, offset + 100)],
                "next_cursor": str(offset + 100) if offset < 9900 else None,
            },
        }

    output = tmp_path / "manifest.json"
    result = prepare_batch(
        {"languages": ["languages:unknown"], "limit": 100},
        {"languages": ["languages:ja"]},
        "classifications.fill",
        output,
        query,
    )
    assert result["total"] == 10000 and len(seen_queries) == 100
    calls = json.loads(output.read_text())["calls"]
    assert calls[-1]["arguments"] == {"work_id": "work-9999", "languages": ["languages:ja"]}
    assert output.stat().st_mode & 0o077 == 0
    with pytest.raises(ValueError, match="already exists"):
        prepare_batch({}, {}, "classifications.fill", output, query)


def test_prepare_discover_deduplicates_and_preserves_intake_options(tmp_path: Path) -> None:
    def query(name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        return {
            "ok": True,
            "result": {"items": [{"provider": "fixture", "external_id": "one", "code": "A-001"}]},
        }

    output = tmp_path / "manifest.json"
    result = prepare_batch(
        {"provider": "fixture"},
        {"tags": ["forum"], "download": True},
        "intake.item",
        output,
        query,
        source_pages=2,
    )
    assert result["total"] == 1
    assert json.loads(output.read_text())["calls"][0]["arguments"] == {
        "code": "A-001",
        "tags": ["forum"],
        "download": True,
    }


def test_remote_connection_error_keeps_receipt_unknown_and_blocks_later_items(tmp_path: Path) -> None:
    manifest = tmp_path / "batch.json"
    receipt = tmp_path / "receipt.jsonl"
    manifest.write_text('{"calls":[{"arguments":{"code":"A-001"}},{"arguments":{"code":"B-002"}}]}')
    calls: list[str] = []

    def call(name: str, arguments: dict[str, JsonValue]) -> JsonValue:
        calls.append(str(arguments["code"]))
        return {"ok": False, "error": {"code": "connection_error"}}

    result = run_batch(manifest, receipt, call, pace_seconds=0)
    assert result["counts"] == {"unknown": 1, "pending": 1} and calls == ["A-001"]
    # A torn final append must not erase the durable unknown checkpoint.
    with receipt.open("ab") as stream:
        stream.write(b'{"index":0,"item":')
    assert run_batch(manifest, receipt, call, retry_failed=True, pace_seconds=0)["counts"] == result["counts"]
    assert calls == ["A-001"]
