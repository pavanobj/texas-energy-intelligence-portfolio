"""Canonical local contract for a 24-hour ERCOT demand forecast curve."""

from __future__ import annotations

import numpy as np
import pandas as pd


FORECAST_HORIZONS = tuple(range(1, 25))
REQUIRED_FORECAST_COLUMNS = {
    "model_id",
    "forecast_origin_utc",
    "horizon_hours",
    "target_timestamp_utc",
    "predicted_demand_mw",
    "feature_as_of_utc",
}


class ForecastCurveContractError(ValueError):
    """A forecast cannot be represented as one complete, causal daily curve."""


def validate_forecast_curve(frame: pd.DataFrame, *, require_complete_curve: bool = True) -> pd.DataFrame:
    """Validate and normalize a long-form Forecast Contract V2 frame.

    Actuals are optional because live forecasts are issued before targets exist.
    The function never supplies actuals, weather issue times, or model metadata.
    """

    missing = REQUIRED_FORECAST_COLUMNS.difference(frame.columns)
    if missing:
        raise ForecastCurveContractError(f"Forecast frame lacks columns: {sorted(missing)}")
    checked = frame.copy()
    for column in ("forecast_origin_utc", "target_timestamp_utc", "feature_as_of_utc"):
        checked[column] = pd.to_datetime(checked[column], utc=True, errors="raise")
    checked["horizon_hours"] = pd.to_numeric(checked["horizon_hours"], errors="raise")
    if not checked["horizon_hours"].isin(FORECAST_HORIZONS).all():
        raise ForecastCurveContractError("horizon_hours must be an integer from 1 through 24.")
    if not np.array_equal(checked["horizon_hours"].to_numpy(), checked["horizon_hours"].astype(int).to_numpy()):
        raise ForecastCurveContractError("horizon_hours must contain whole hours.")
    checked["horizon_hours"] = checked["horizon_hours"].astype("int8")
    expected_target = checked["forecast_origin_utc"] + pd.to_timedelta(checked["horizon_hours"], unit="h")
    if not checked["target_timestamp_utc"].eq(expected_target).all():
        raise ForecastCurveContractError("target_timestamp_utc must equal origin plus horizon_hours.")
    if (checked["feature_as_of_utc"] > checked["forecast_origin_utc"]).any():
        raise ForecastCurveContractError("feature_as_of_utc cannot be later than forecast origin.")
    for column in ("predicted_demand_mw", "actual_demand_mw"):
        if column in checked:
            checked[column] = pd.to_numeric(checked[column], errors="raise")
            if column == "predicted_demand_mw" and not np.isfinite(checked[column]).all():
                raise ForecastCurveContractError("predicted_demand_mw must be finite.")
            if column == "actual_demand_mw" and not np.isfinite(checked[column].dropna()).all():
                raise ForecastCurveContractError("actual_demand_mw must be finite when present.")
    identity = ["model_id", "forecast_origin_utc", "horizon_hours"]
    if checked.duplicated(identity).any():
        raise ForecastCurveContractError("Forecast frame contains duplicate model/origin/horizon predictions.")
    if require_complete_curve:
        counts = checked.groupby(["model_id", "forecast_origin_utc"])["horizon_hours"].agg(lambda x: tuple(sorted(x)))
        if not counts.map(lambda horizons: horizons == FORECAST_HORIZONS).all():
            raise ForecastCurveContractError("Every forecast origin must contain exactly horizons 1 through 24.")
    checked["demand_unit"] = "MW"
    return checked.sort_values(identity).reset_index(drop=True)
