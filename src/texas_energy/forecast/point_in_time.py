"""Point-in-time availability rules shared by forecasting feature builders.

Event time is not availability time.  Until an authoritative historical EIA
publication-time feed is retained, the platform uses a conservative 24-hour
cutoff for ERCOT demand.  It is explicit configuration rather than an implied
property of a timestamp and may be tightened only with source evidence.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd


DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS = 24


class PointInTimeError(ValueError):
    """A feature would not have been available at forecast issuance."""


@dataclass(frozen=True)
class DemandFreshnessObservation:
    """One observed source-freshness measurement, not a publication SLA.

    EIA's RTO endpoint exposes event timestamps but no source publication
    timestamp.  A scheduled probe may record ``retrieved_at_utc`` and the
    newest validated period returned, which makes observed latency auditable.
    It must not be represented as EIA's official availability guarantee.
    """

    retrieved_at_utc: pd.Timestamp
    newest_source_period_utc: pd.Timestamp
    observed_latency_hours: float


def observed_demand_freshness(
    retrieved_at_utc: object,
    newest_source_period_utc: object,
) -> DemandFreshnessObservation:
    """Calculate a measured event-to-retrieval delay without changing policy."""

    retrieved = pd.to_datetime(retrieved_at_utc, utc=True, errors="raise")
    newest = pd.to_datetime(newest_source_period_utc, utc=True, errors="raise")
    if newest > retrieved:
        raise PointInTimeError("newest_source_period_utc cannot be later than retrieval time.")
    latency = (retrieved - newest) / pd.Timedelta(hours=1)
    return DemandFreshnessObservation(retrieved, newest, float(latency))


def demand_available_through(
    forecast_origin_utc: object,
    *,
    availability_lag_hours: int = DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS,
) -> pd.Timestamp:
    """Return the latest demand event time usable at an issuance time."""

    if isinstance(availability_lag_hours, bool) or availability_lag_hours < 1:
        raise PointInTimeError("availability_lag_hours must be a positive whole number.")
    origin = pd.to_datetime(forecast_origin_utc, utc=True, errors="raise")
    return origin - pd.Timedelta(hours=availability_lag_hours)


def require_demand_as_of(
    event_timestamp_utc: object,
    forecast_origin_utc: object,
    *,
    availability_lag_hours: int = DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS,
) -> None:
    """Fail if an ERCOT event-time value is too recent for the issuance."""

    event_time = pd.to_datetime(event_timestamp_utc, utc=True, errors="raise")
    cutoff = demand_available_through(
        forecast_origin_utc, availability_lag_hours=availability_lag_hours
    )
    if event_time > cutoff:
        raise PointInTimeError("Demand event time is newer than the configured availability cutoff.")
