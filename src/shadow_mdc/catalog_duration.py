"""Duration bands derived only from a work's known runtime, in seconds."""

from dataclasses import dataclass

from sqlalchemy import ColumnElement, and_, case, or_, true

from .db.models import Work


@dataclass(frozen=True)
class DurationBand:
    id: str
    name: str
    minimum: int | None
    maximum: int | None


DURATION_BANDS = (
    DurationBand("under30", "30 分钟以下", None, 1800),
    DurationBand("30to60", "30-60 分钟", 1800, 3600),
    DurationBand("60to120", "60-120 分钟", 3600, 7200),
    DurationBand("over120", "120 分钟及以上", 7200, None),
    DurationBand("unknown", "时长待补", None, None),
)


def work_duration() -> ColumnElement[int]:
    """Zero/negative metadata is missing, never a real short work."""
    return case((Work.runtime_seconds > 0, Work.runtime_seconds), else_=None)


def duration_condition(runtime: ColumnElement[int], band: DurationBand) -> ColumnElement[bool]:
    """Share band boundaries across a single aggregate query."""
    if band.id == "unknown":
        return or_(runtime.is_(None), runtime <= 0)
    return and_(
        runtime > 0,
        runtime >= band.minimum if band.minimum is not None else true(),
        runtime < band.maximum if band.maximum is not None else true(),
    )
