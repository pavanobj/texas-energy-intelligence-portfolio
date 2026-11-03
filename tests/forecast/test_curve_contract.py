from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.curve_contract import ForecastCurveContractError, validate_forecast_curve


def _curve() -> pd.DataFrame:
    origin = pd.Timestamp("2025-01-01T00:00:00Z")
    return pd.DataFrame({
        "model_id": ["candidate"] * 24,
        "forecast_origin_utc": [origin] * 24,
        "horizon_hours": list(range(1, 25)),
        "target_timestamp_utc": [origin + pd.Timedelta(hours=hour) for hour in range(1, 25)],
        "predicted_demand_mw": range(24),
        "actual_demand_mw": range(24),
        "feature_as_of_utc": [origin] * 24,
    })


def test_complete_curve_has_utc_targets_and_mw_unit() -> None:
    result = validate_forecast_curve(_curve())
    assert result["demand_unit"].eq("MW").all()
    assert result["target_timestamp_utc"].dt.tz is not None


def test_curve_rejects_missing_horizon_and_future_feature() -> None:
    missing = _curve().iloc[:-1]
    with pytest.raises(ForecastCurveContractError, match="exactly horizons"):
        validate_forecast_curve(missing)
    future = _curve()
    future.loc[0, "feature_as_of_utc"] = future.loc[0, "forecast_origin_utc"] + pd.Timedelta(hours=1)
    with pytest.raises(ForecastCurveContractError, match="feature_as_of"):
        validate_forecast_curve(future)
