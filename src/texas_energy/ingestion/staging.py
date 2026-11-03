"""Offline planning and quarantine decisions for bounded historical stages.

This module deliberately does not call providers, S3, Spark, or the AWS SDK.
It supplies deterministic monthly work units around the existing seven-day
backfill runner, so operators can review and execute one stage at a time.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, timedelta

from texas_energy.data.coverage import HistoricalCoverageAudit


@dataclass(frozen=True)
class HistoricalStage:
    """An inclusive UTC calendar month selected for one controlled run."""

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.start_date.day != 1:
            raise ValueError("Historical stages must start on the first day of a month.")
        if self.end_date < self.start_date:
            raise ValueError("Historical stage end_date must not precede start_date.")

    @property
    def identifier(self) -> str:
        return self.start_date.strftime("%Y-%m")


@dataclass(frozen=True)
class StagePlan:
    """Reviewable sequence that never re-runs explicitly completed months."""

    pending: tuple[HistoricalStage, ...]
    skipped_completed: tuple[HistoricalStage, ...]


@dataclass(frozen=True)
class StageDisposition:
    """Coverage-gate outcome; quarantining never deletes valid Bronze data."""

    accepted: bool
    reason: str


def build_monthly_stage_plan(
    *, start_date: date, end_date: date, completed_months: frozenset[str] = frozenset()
) -> StagePlan:
    """Return complete calendar-month stages within an inclusive UTC range.

    ``completed_months`` must contain ``YYYY-MM`` identifiers only after the
    relevant Bronze coverage audit has passed.  It is an explicit operator
    input, not inferred from local files or a partially written ledger.
    """

    if start_date.day != 1:
        raise ValueError("start_date must be the first day of a month.")
    if end_date < start_date:
        raise ValueError("end_date must not precede start_date.")
    stage_start = start_date
    pending: list[HistoricalStage] = []
    skipped: list[HistoricalStage] = []
    while stage_start <= end_date:
        next_month = _next_month(stage_start)
        stage_end = min(end_date, next_month - timedelta(days=1))
        stage = HistoricalStage(stage_start, stage_end)
        (skipped if stage.identifier in completed_months else pending).append(stage)
        stage_start = next_month
    return StagePlan(tuple(pending), tuple(skipped))


def classify_coverage(audit: HistoricalCoverageAudit) -> StageDisposition:
    """Fail closed on gaps, duplicates, or source revisions.

    The affected month is quarantined for investigation.  Other completed,
    independently audited stages remain valid and are never discarded.
    """

    issues = {
        "missing demand hours": len(audit.missing_demand_hours),
        "missing weather city-hours": len(audit.missing_weather_city_hours),
        "duplicate demand keys": len(audit.duplicate_demand_hours),
        "duplicate weather keys": len(audit.duplicate_weather_city_hours),
        "source revisions": len(audit.source_revisions),
        "unexpected demand hours": len(audit.unexpected_demand_hours),
        "unexpected weather city-hours": len(audit.unexpected_weather_city_hours),
    }
    failed = {name: count for name, count in issues.items() if count}
    if failed:
        details = ", ".join(f"{name}={count}" for name, count in failed.items())
        return StageDisposition(False, f"quarantine required: {details}")
    return StageDisposition(True, "coverage accepted")


def _next_month(value: date) -> date:
    if value.month == 12:
        return date(value.year + 1, 1, 1)
    return date(value.year, value.month + 1, 1)
