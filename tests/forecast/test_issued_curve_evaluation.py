from __future__ import annotations

import pandas as pd

from texas_energy.forecast.chronos2 import build_issued_curve_origins
from texas_energy.forecast.issued_curve_evaluation import completed_origins, pending_issued_origins


def _origins():
    data = pd.DataFrame({
        "period_utc": pd.date_range("2024-01-01", periods=100, freq="h", tz="UTC"),
        "observed_average_demand_mw": range(100, 200),
    })
    return build_issued_curve_origins(
        data, forecast_start_utc="2024-01-04T00:00:00Z", forecast_end_utc="2024-01-04T01:00:00Z",
        context_hours=24, demand_availability_lag_hours=24,
    )


def _part(origin, horizons: range) -> pd.DataFrame:
    return pd.DataFrame({
        "forecast_origin_utc": [origin.forecast_origin_utc] * len(horizons),
        "target_timestamp_utc": [origin.forecast_origin_utc + pd.Timedelta(hours=h) for h in horizons],
        "horizon_hours": list(horizons), "predicted_demand_mw": [1.0] * len(horizons),
        "actual_demand_mw": [2.0] * len(horizons),
    })


def test_partial_curve_is_not_a_resume_checkpoint() -> None:
    origins = _origins()
    complete = completed_origins([_part(origins[0], range(1, 24))])
    assert complete == set()
    assert pending_issued_origins(origins, complete) == origins


def test_complete_curve_is_a_resume_checkpoint() -> None:
    origins = _origins()
    complete = completed_origins([_part(origins[0], range(1, 25))])
    assert complete == {origins[0].forecast_origin_utc}
    assert pending_issued_origins(origins, complete) == (origins[1],)
