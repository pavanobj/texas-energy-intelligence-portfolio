from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.point_in_time import (
    PointInTimeError,
    demand_available_through,
    observed_demand_freshness,
    require_demand_as_of,
)


def test_demand_cutoff_is_conservative_and_utc() -> None:
    origin = pd.Timestamp("2025-01-02T00:00:00Z")
    assert demand_available_through(origin) == pd.Timestamp("2025-01-01T00:00:00Z")
    require_demand_as_of("2025-01-01T00:00:00Z", origin)
    with pytest.raises(PointInTimeError):
        require_demand_as_of("2025-01-01T01:00:00Z", origin)


def test_observed_freshness_is_measured_separately_from_policy() -> None:
    observation = observed_demand_freshness("2025-01-02T03:30:00Z", "2025-01-02T01:00:00Z")
    assert observation.observed_latency_hours == 2.5
    assert demand_available_through("2025-01-02T03:30:00Z") == pd.Timestamp("2025-01-01T03:30:00Z")
