"""Strict adapters and fair common-origin comparison for 24-hour curves."""

from __future__ import annotations

import pandas as pd

from .curve_contract import ForecastCurveContractError, validate_forecast_curve


def canonicalize_candidate(
    frame: pd.DataFrame, *, model_id: str, candidate_id: str, feature_as_of_column: str = "context_end_utc"
) -> pd.DataFrame:
    """Normalize saved trainer output without altering a model prediction."""

    required = {"forecast_origin_utc", "target_timestamp_utc", "horizon_hours", "actual_demand_mw", "predicted_demand_mw"}
    if missing := required.difference(frame.columns):
        raise ForecastCurveContractError(f"Candidate output lacks columns: {sorted(missing)}")
    result = frame.copy()
    result["model_id"] = model_id
    result["candidate_id"] = candidate_id
    if feature_as_of_column in result:
        result["feature_as_of_utc"] = result[feature_as_of_column]
    elif "feature_as_of_utc" not in result:
        # Demand-only direct curves persist their latest allowed demand value
        # as context_end_utc.  Requiring an explicit column is safer than
        # guessing that issue-time features were available.
        raise ForecastCurveContractError("Candidate must persist feature_as_of_utc or context_end_utc.")
    result = validate_forecast_curve(result)
    return result


def strict_common_curve_rows(frame: pd.DataFrame, *, model_ids: tuple[str, ...]) -> pd.DataFrame:
    """Keep only exact origin/horizon targets present for every candidate."""

    checked = validate_forecast_curve(frame)
    keys = ["forecast_origin_utc", "target_timestamp_utc", "horizon_hours"]
    present = checked.loc[checked.model_id.isin(model_ids)].groupby(keys)["model_id"].nunique()
    common = present.loc[present.eq(len(model_ids))].index
    result = checked.set_index(keys).loc[common].reset_index()
    result = result.loc[result.model_id.isin(model_ids)].copy()
    # Targets and actuals must agree before any metric is calculated.
    agreement = result.groupby(keys)["actual_demand_mw"].nunique()
    if not agreement.eq(1).all():
        raise ForecastCurveContractError("Candidates disagree on actual values for a shared target.")
    return result.sort_values(["forecast_origin_utc", "horizon_hours", "model_id"]).reset_index(drop=True)
