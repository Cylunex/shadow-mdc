"""Magnet failover / offline recovery (ideas only; original implementation).

One user-visible offline task can try several magnets. A durable Recovery
checkpoint tracks attempts, the active hash, stall/exhaustion, and in-flight
cancel/switch/submit actions. A replacement is never submitted until removal of
its predecessor has succeeded. Stall/attempt thresholds are internal policy;
the only user preference is ``auto_switch``.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any, Literal

from ..db.models import PanOfflineTask, WorkMagnet, utc_now
from ..media.magnets import magnet_sort_key

Action = Literal["", "cancel", "switch", "submit"]

# Internal recovery policy (not user-configurable).
ZERO_PROGRESS_TIMEOUT = timedelta(minutes=15)
STALLED_TIMEOUT = timedelta(minutes=30)
COMPLETION_GRACE = timedelta(minutes=60)
MAX_MAGNET_ATTEMPTS = 3
# Progress observations older than this are ignored for stall detection.
OBSERVATION_GAP = timedelta(seconds=90)

SWITCH_REASON_FAILED = "remote_failed"
SWITCH_REASON_STALLED = "stalled"
SWITCH_REASON_MANUAL = "manual_next"
SWITCH_REASON_EXHAUSTED = "exhausted"
SWITCH_REASON_CANCELLED = "cancelled"


@dataclass(slots=True)
class RecoveryPolicy:
    zero_progress_timeout: timedelta = ZERO_PROGRESS_TIMEOUT
    stalled_timeout: timedelta = STALLED_TIMEOUT
    completion_grace: timedelta = COMPLETION_GRACE
    max_attempts: int = MAX_MAGNET_ATTEMPTS

    def timeout_for(self, progress: float) -> timedelta:
        if progress <= 0.0:
            return self.zero_progress_timeout
        if progress >= 95.0:
            return max(self.stalled_timeout, self.completion_grace)
        return self.stalled_timeout


DEFAULT_POLICY = RecoveryPolicy()


@dataclass(slots=True)
class RecoveryAttempt:
    info_hash: str
    magnet_id: str | None = None
    url: str | None = None
    reason: str | None = None
    started_at: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "info_hash": self.info_hash,
            "magnet_id": self.magnet_id,
            "url": self.url,
            "reason": self.reason,
            "started_at": self.started_at,
        }

    @classmethod
    def from_dict(cls, raw: dict[str, Any]) -> RecoveryAttempt:
        return cls(
            info_hash=str(raw.get("info_hash") or "").upper(),
            magnet_id=_opt_str(raw.get("magnet_id")),
            url=_opt_str(raw.get("url")),
            reason=_opt_str(raw.get("reason")),
            started_at=str(raw.get("started_at") or ""),
        )


@dataclass(slots=True)
class RecoveryCheckpoint:
    attempts: list[RecoveryAttempt] = field(default_factory=list)
    current_hash: str = ""
    action: Action = ""
    next_hash: str | None = None
    next_magnet_id: str | None = None
    next_url: str | None = None
    retry_at: str | None = None
    observed_at: str | None = None
    progress_at: str | None = None
    progress: float = 0.0
    stalled: bool = False
    exhausted: bool = False
    reason: str | None = None
    removal_started: bool = False
    submission_started: bool = False
    manual: bool = False

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["attempts"] = [item.to_dict() for item in self.attempts]
        return payload

    @classmethod
    def from_dict(cls, raw: dict[str, Any] | None) -> RecoveryCheckpoint:
        if not isinstance(raw, dict):
            return cls()
        attempts_raw = raw.get("attempts") or []
        attempts = [
            RecoveryAttempt.from_dict(item) for item in attempts_raw if isinstance(item, dict)
        ]
        action_raw = str(raw.get("action") or "")
        action: Action = action_raw if action_raw in {"", "cancel", "switch", "submit"} else ""
        return cls(
            attempts=attempts,
            current_hash=str(raw.get("current_hash") or "").upper(),
            action=action,
            next_hash=_opt_str(raw.get("next_hash")),
            next_magnet_id=_opt_str(raw.get("next_magnet_id")),
            next_url=_opt_str(raw.get("next_url")),
            retry_at=_opt_str(raw.get("retry_at")),
            observed_at=_opt_str(raw.get("observed_at")),
            progress_at=_opt_str(raw.get("progress_at")),
            progress=float(raw.get("progress") or 0.0),
            stalled=bool(raw.get("stalled")),
            exhausted=bool(raw.get("exhausted")),
            reason=_opt_str(raw.get("reason")),
            removal_started=bool(raw.get("removal_started")),
            submission_started=bool(raw.get("submission_started")),
            manual=bool(raw.get("manual")),
        )


@dataclass(frozen=True, slots=True)
class OfflineProjection:
    download_state: str
    attempt_count: int
    switch_reason: str | None
    can_cancel: bool
    can_switch: bool


def _opt_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _parse_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def now_iso(clock: datetime | None = None) -> str:
    stamp = clock or utc_now()
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    return stamp.isoformat()


def seed_recovery(
    *,
    info_hash: str,
    magnet_id: str | None,
    url: str | None,
    started_at: datetime | None = None,
) -> RecoveryCheckpoint:
    digest = info_hash.strip().upper()
    started = now_iso(started_at)
    return RecoveryCheckpoint(
        attempts=[
            RecoveryAttempt(
                info_hash=digest,
                magnet_id=magnet_id,
                url=url,
                started_at=started,
            )
        ],
        current_hash=digest,
        progress_at=started,
        observed_at=started,
        progress=0.0,
    )


def load_recovery(task: PanOfflineTask) -> RecoveryCheckpoint:
    raw = getattr(task, "recovery_json", None)
    if isinstance(raw, dict) and raw:
        state = RecoveryCheckpoint.from_dict(raw)
        if state.current_hash:
            return state
    return seed_recovery(
        info_hash=task.info_hash,
        magnet_id=task.magnet_id,
        url=task.url,
        started_at=task.created_at,
    )


def attempted_hashes(state: RecoveryCheckpoint) -> set[str]:
    return {item.info_hash.upper() for item in state.attempts if item.info_hash}


def rank_candidate_magnets(
    magnets: list[WorkMagnet],
    *,
    expected_code: str | None = None,
) -> list[WorkMagnet]:
    return sorted(
        magnets,
        key=lambda item: magnet_sort_key(
            name=item.name,
            size_bytes=item.size_bytes,
            has_subtitle=bool(item.has_subtitle),
            hd=bool(item.hd),
            expected_code=expected_code,
        ),
        reverse=True,
    )


def next_magnet_candidate(
    magnets: list[WorkMagnet],
    state: RecoveryCheckpoint,
    *,
    policy: RecoveryPolicy = DEFAULT_POLICY,
    exclude_hashes: set[str] | None = None,
    expected_code: str | None = None,
) -> WorkMagnet | None:
    used = attempted_hashes(state)
    if exclude_hashes:
        used |= {item.strip().upper() for item in exclude_hashes if item}
    if len(state.attempts) >= policy.max_attempts:
        return None
    for magnet in rank_candidate_magnets(magnets, expected_code=expected_code):
        digest = (magnet.info_hash or "").strip().upper()
        if not digest or digest in used:
            continue
        return magnet
    return None


def observe_progress(
    state: RecoveryCheckpoint,
    *,
    progress: float,
    clock: datetime | None = None,
) -> RecoveryCheckpoint:
    """Update observation timestamps; bump progress_at only when progress moves."""

    stamp = now_iso(clock)
    previous = state.progress
    state.observed_at = stamp
    state.progress = float(progress)
    if state.progress_at is None or abs(state.progress - previous) >= 0.05:
        state.progress_at = stamp
        state.stalled = False
    return state


def detect_stall_or_fail(
    state: RecoveryCheckpoint,
    *,
    remote_failed: bool,
    remote_running: bool,
    auto_switch: bool,
    clock: datetime | None = None,
    policy: RecoveryPolicy = DEFAULT_POLICY,
) -> str | None:
    """Return a switch reason when recovery should start, else None."""

    if state.exhausted or state.action:
        return None
    if remote_failed:
        return SWITCH_REASON_FAILED if auto_switch else None
    if not remote_running or not auto_switch:
        return None
    now = clock or utc_now()
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    observed = _parse_iso(state.observed_at)
    progressed = _parse_iso(state.progress_at)
    if observed is None or progressed is None:
        return None
    if now - observed > OBSERVATION_GAP:
        return None
    if now - progressed >= policy.timeout_for(state.progress):
        state.stalled = True
        return SWITCH_REASON_STALLED
    return None


def begin_switch(
    state: RecoveryCheckpoint,
    *,
    reason: str,
    next_magnet: WorkMagnet | None,
    manual: bool = False,
) -> RecoveryCheckpoint:
    if next_magnet is None:
        state.exhausted = True
        state.stalled = False
        state.action = ""
        state.reason = SWITCH_REASON_EXHAUSTED
        state.next_hash = None
        state.next_magnet_id = None
        state.next_url = None
        state.manual = manual
        return state
    digest = (next_magnet.info_hash or "").strip().upper()
    state.action = "switch"
    state.reason = reason
    state.next_hash = digest
    state.next_magnet_id = next_magnet.id
    state.next_url = next_magnet.uri
    state.removal_started = False
    state.submission_started = False
    state.manual = manual
    state.stalled = reason == SWITCH_REASON_STALLED
    state.exhausted = False
    return state


def begin_cancel(state: RecoveryCheckpoint, *, manual: bool = True) -> RecoveryCheckpoint:
    state.action = "cancel"
    state.reason = SWITCH_REASON_CANCELLED
    state.manual = manual
    state.removal_started = False
    state.submission_started = False
    state.next_hash = None
    state.next_magnet_id = None
    state.next_url = None
    return state


def mark_removal_started(state: RecoveryCheckpoint) -> RecoveryCheckpoint:
    state.removal_started = True
    return state


def after_removal_for_switch(state: RecoveryCheckpoint) -> RecoveryCheckpoint:
    """Predecessor gone: promote to submit of the queued next magnet."""

    state.action = "submit"
    state.removal_started = False
    state.submission_started = False
    return state


def after_submit(
    state: RecoveryCheckpoint,
    *,
    info_hash: str,
    magnet_id: str | None,
    url: str | None,
    clock: datetime | None = None,
) -> RecoveryCheckpoint:
    digest = info_hash.strip().upper()
    started = now_iso(clock)
    state.attempts.append(
        RecoveryAttempt(
            info_hash=digest,
            magnet_id=magnet_id,
            url=url,
            reason=state.reason,
            started_at=started,
        )
    )
    state.current_hash = digest
    state.action = ""
    state.next_hash = None
    state.next_magnet_id = None
    state.next_url = None
    state.removal_started = False
    state.submission_started = False
    state.stalled = False
    state.progress = 0.0
    state.progress_at = started
    state.observed_at = started
    return state


def project_offline(task: PanOfflineTask, *, now: datetime | None = None) -> OfflineProjection:
    state = load_recovery(task)
    can_cancel = task.status in {"running", "failed"}
    can_switch = can_cancel and state.action == "" and not state.exhausted
    attempt_count = max(1, len(state.attempts)) if task.info_hash else len(state.attempts)
    switch_reason = state.reason
    download_state = ""
    if task.status == "cancelled":
        download_state = "cancelled"
        can_cancel = False
        can_switch = False
    elif state.action == "cancel":
        download_state = "cancelling"
        can_switch = False
    elif state.action == "switch":
        download_state = "switching"
        can_switch = False
    elif state.action == "submit":
        download_state = "submitting"
        can_switch = False
    elif state.exhausted:
        download_state = "exhausted"
        can_switch = False
    elif state.stalled and task.status == "running":
        download_state = "stalled"
    elif task.status == "running":
        download_state = "queued"
        retry_at = _parse_iso(state.retry_at)
        clock = now or utc_now()
        if clock.tzinfo is None:
            clock = clock.replace(tzinfo=UTC)
        if retry_at is not None and retry_at > clock:
            download_state = "waiting"
    elif task.status == "failed":
        download_state = "failed"
    elif task.status == "done":
        download_state = "done"
        can_cancel = False
        can_switch = False
    if not can_cancel and download_state in {"cancelling", "switching", "submitting"}:
        pass
    elif not can_cancel and task.status not in {"cancelled", "done", "failed"}:
        download_state = ""
    return OfflineProjection(
        download_state=download_state,
        attempt_count=attempt_count,
        switch_reason=switch_reason,
        can_cancel=can_cancel,
        can_switch=can_switch,
    )


def stream_cache_expiry(
    url: str, *, now: datetime | None = None, margin_seconds: float = 30.0
) -> datetime | None:
    """Parse 115 ``t`` / ``expires`` Unix query params; None when unknown (do not cache)."""

    from urllib.parse import parse_qs, urlparse

    stamp = now or datetime.now(UTC)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=UTC)
    try:
        parsed = urlparse(url)
        query = parse_qs(parsed.query)
    except ValueError:
        return None
    known = False
    # Soft upper bound when link carries a long-lived signature.
    expires = stamp + timedelta(minutes=1)
    for name in ("t", "expires"):
        for value in query.get(name, []):
            try:
                seconds = int(value)
            except (TypeError, ValueError):
                return None
            if seconds <= 0:
                return None
            deadline = datetime.fromtimestamp(seconds, tz=UTC) - timedelta(seconds=margin_seconds)
            if deadline < expires:
                expires = deadline
            known = True
    if not known:
        return None
    if expires <= stamp:
        return None
    return expires
