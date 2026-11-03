"""Durable bookkeeping for 24-step issued-forecast evaluations.

This module deliberately contains no model-specific inference.  It defines the
resume boundary shared by Chronos and future curve forecasters: an origin is
complete only when its durable part contains every requested horizon exactly
once.  A crashed write therefore causes the whole origin to be recomputed,
never a silently truncated forecast curve.
"""

from __future__ import annotations

from collections.abc import Iterable

import pandas as pd


class IssuedCurveEvaluationError(ValueError):
    """Persisted curve outputs do not satisfy the forecast contract."""


REQUIRED_COLUMNS = frozenset(
    {
        "forecast_origin_utc",
        "target_timestamp_utc",
        "horizon_hours",
        "predicted_demand_mw",
        "actual_demand_mw",
    }
)


def completed_origins(parts: Iterable[pd.DataFrame], *, horizon_hours: int = 24) -> set[pd.Timestamp]:
    """Return only origins with a complete, internally consistent curve."""

    if horizon_hours < 1:
        raise IssuedCurveEvaluationError("horizon_hours must be positive.")
    completed: set[pd.Timestamp] = set()
    for part in parts:
        missing = REQUIRED_COLUMNS.difference(part.columns)
        if missing:
            raise IssuedCurveEvaluationError(f"Persisted curve part lacks columns: {sorted(missing)}")
        frame = part.copy()
        frame["forecast_origin_utc"] = pd.to_datetime(frame["forecast_origin_utc"], utc=True, errors="coerce")
        frame["target_timestamp_utc"] = pd.to_datetime(frame["target_timestamp_utc"], utc=True, errors="coerce")
        if frame[["forecast_origin_utc", "target_timestamp_utc"]].isna().any().any():
            raise IssuedCurveEvaluationError("Persisted curve timestamps must be UTC-parseable.")
        for origin, group in frame.groupby("forecast_origin_utc", sort=False):
            if len(group) != horizon_hours or group["horizon_hours"].duplicated().any():
                continue
            expected_horizons = list(range(1, horizon_hours + 1))
            ordered = group.sort_values("horizon_hours")
            if ordered["horizon_hours"].tolist() != expected_horizons:
                continue
            expected_targets = origin + pd.to_timedelta(expected_horizons, unit="h")
            if not ordered["target_timestamp_utc"].reset_index(drop=True).equals(pd.Series(expected_targets)):
                continue
            completed.add(origin)
    return completed


def pending_issued_origins(origins: Iterable[object], complete: set[pd.Timestamp]) -> tuple[object, ...]:
    """Keep origins that are not yet durably represented by all horizons."""

    return tuple(
        origin
        for origin in origins
        if pd.to_datetime(getattr(origin, "forecast_origin_utc"), utc=True) not in complete
    )
