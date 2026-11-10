"""Sequential CLI batches with private, atomic per-item receipts and explicit resumption."""

from __future__ import annotations

import fcntl
import hashlib
import os
import time
from collections import Counter
from collections.abc import Callable
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, JsonValue


class BatchCall(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(default="intake.item", min_length=1, max_length=100)
    arguments: dict[str, JsonValue]


class BatchManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    calls: list[BatchCall] = Field(min_length=1, max_length=50000)


class BatchItem(BaseModel):
    state: Literal["pending", "unknown", "done", "failed"] = "pending"
    outcome: str = "pending"
    result: JsonValue = None


class BatchReceipt(BaseModel):
    digest: str
    items: list[BatchItem]


class ReceiptHeader(BaseModel):
    digest: str
    total: int


class ReceiptEvent(BaseModel):
    index: int = Field(ge=0)
    item: BatchItem


def _append(path: Path, record: ReceiptHeader | ReceiptEvent) -> None:
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        os.fchmod(stream.fileno(), 0o600)
        stream.write(record.model_dump_json() + "\n")
        stream.flush()
        os.fsync(stream.fileno())


def _load_receipt(path: Path, digest: str, total: int) -> BatchReceipt:
    if path.stat().st_mode & 0o077:
        raise ValueError("batch receipt must have permissions 0600")
    data = path.read_bytes()
    boundary = data.rfind(b"\n") + 1
    if boundary == 0:
        raise ValueError("batch receipt header is incomplete")
    lines = data[:boundary].splitlines()
    header = ReceiptHeader.model_validate_json(lines[0])
    if header.digest != digest or header.total != total:
        raise ValueError("manifest changed; use a separate receipt for a new batch")
    receipt = BatchReceipt(digest=digest, items=[BatchItem() for _ in range(total)])
    for line in lines[1:]:
        event = ReceiptEvent.model_validate_json(line)
        if event.index >= total:
            raise ValueError("invalid batch receipt item")
        receipt.items[event.index] = event.item
    if boundary < len(data):
        # The previous complete event is authoritative after an interrupted append.
        # A result append is always preceded by a durable unknown event.
        with path.open("r+b") as stream:
            stream.truncate(boundary)
    return receipt


def run_batch(
    manifest_path: Path,
    receipt_path: Path,
    call: Callable[[str, dict[str, JsonValue]], JsonValue],
    *,
    retry_failed: bool = False,
    retry_unknown: bool = False,
    pace_seconds: float = 0.25,
) -> dict[str, JsonValue]:
    """Run each call once; leave interrupted writes unknown until the caller reconciles them."""
    manifest = BatchManifest.model_validate_json(manifest_path.read_text())
    digest = hashlib.sha256(manifest.model_dump_json().encode()).hexdigest()
    receipt_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_path = receipt_path.with_name(receipt_path.name + ".lock")
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    with os.fdopen(lock_fd, "w") as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if receipt_path.exists():
            receipt = _load_receipt(receipt_path, digest, len(manifest.calls))
        else:
            receipt = BatchReceipt(digest=digest, items=[BatchItem() for _ in manifest.calls])
            _append(receipt_path, ReceiptHeader(digest=digest, total=len(manifest.calls)))
        for index, (operation, item) in enumerate(zip(manifest.calls, receipt.items, strict=True)):
            if item.state == "done" or (item.state == "failed" and not retry_failed):
                continue
            if item.state == "unknown" and not retry_unknown:
                break
            item.state, item.outcome = "unknown", "unknown"
            _append(receipt_path, ReceiptEvent(index=index, item=item))
            # Exceptions deliberately leave the checkpoint unknown; never blindly replay writes.
            result = call(operation.name, operation.arguments)
            item.result = result
            body = result.get("result") if isinstance(result, dict) else None
            error = result.get("error") if isinstance(result, dict) else None
            status = result.get("status") if isinstance(result, dict) else None
            if (isinstance(status, int) and status >= 500) or (
                isinstance(error, dict) and error.get("code") == "connection_error"
            ):
                _append(receipt_path, ReceiptEvent(index=index, item=item))
                break
            if (isinstance(result, dict) and result.get("ok") is False) or (
                isinstance(body, dict) and body.get("outcome") in {"failed", "error"}
            ):
                item.state, item.outcome = "failed", "failed"
            else:
                item.state = "done"
                item.outcome = (
                    str(body.get("outcome") or body.get("action") or "completed")
                    if isinstance(body, dict)
                    else "completed"
                )
            _append(receipt_path, ReceiptEvent(index=index, item=item))
            if pace_seconds:
                time.sleep(pace_seconds)
        counts = Counter(item.outcome for item in receipt.items)
        return {
            "ok": all(item.state == "done" for item in receipt.items),
            "total": len(receipt.items),
            "counts": dict(counts),
            "receipt": str(receipt_path.resolve()),
        }


def prepare_batch(
    query: dict[str, JsonValue],
    arguments: dict[str, JsonValue],
    name: str,
    output: Path,
    call: Callable[[str, dict[str, JsonValue]], JsonValue],
    *,
    source_pages: int = 0,
) -> dict[str, JsonValue]:
    """Freeze catalog IDs or discover pages into a reviewable manifest before any business write."""
    if output.exists():
        raise ValueError("manifest already exists; use another output path")
    allowed = {"works.fill", "works.tags", "works.want", "classifications.fill", "classifications.assign"}
    if (source_pages and name != "intake.item") or (not source_pages and name not in allowed):
        raise ValueError("unsupported prepared operation")
    calls: list[BatchCall] = []
    seen: set[str] = set()
    cursors: set[str] = set()
    page = int(str(query.get("page", 1)))
    values = dict(query)
    while True:
        result = call("discover.browse" if source_pages else "works.query", values)
        if not isinstance(result, dict) or result.get("ok") is not True:
            raise ValueError("batch selection failed")
        body = result.get("result")
        if not isinstance(body, dict) or not isinstance(body.get("items"), list):
            raise ValueError("invalid batch selection result")
        items = body["items"]
        assert isinstance(items, list)
        for item in items:
            if not isinstance(item, dict):
                raise ValueError("invalid selection item")
            if source_pages:
                code = item.get("code")
                external_id = item.get("external_id")
                provider = item.get("provider") or query.get("provider", "javdb")
                if isinstance(code, str) and code:
                    identity = code.casefold()
                    args: dict[str, JsonValue] = {**arguments, "code": code}
                elif isinstance(external_id, str) and isinstance(provider, str):
                    identity = provider + ":" + external_id
                    args = {**arguments, "external_ids": {provider: external_id}}
                else:
                    raise ValueError("discover item has no supported identity")
            else:
                work_id = item.get("id")
                if not isinstance(work_id, str):
                    raise ValueError("catalog item has no work ID")
                identity = work_id
                args = {**arguments, "work_id": work_id}
            if identity not in seen:
                seen.add(identity)
                calls.append(BatchCall(name=name, arguments=args))
                if len(calls) > 50000:
                    raise ValueError("split batches larger than 50000 works")
        if source_pages:
            source_pages -= 1
            if not source_pages or not body["items"]:
                break
            page += 1
            values = {**query, "page": page}
        else:
            cursor = body.get("next_cursor")
            if cursor is None:
                break
            if not isinstance(cursor, str) or cursor in cursors:
                raise ValueError("invalid or repeated catalog cursor")
            cursors.add(cursor)
            values = {**query, "cursor": cursor}
    if not calls:
        return {"ok": True, "total": 0, "manifest": None}
    manifest = BatchManifest(calls=calls)
    output.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Exclusive create so preparation cannot silently replace another batch.
    fd = os.open(output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as stream:
        stream.write(manifest.model_dump_json(indent=2))
    return {"ok": True, "total": len(calls), "manifest": str(output.resolve())}
