from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.curve_comparison import canonicalize_candidate, strict_common_curve_rows
from texas_energy.forecast.curve_contract import ForecastCurveContractError


def _candidate(origin: str = "2025-01-01T00:00:00Z") -> pd.DataFrame:
    timestamp = pd.Timestamp(origin)
    return pd.DataFrame({
        "forecast_origin_utc": [timestamp] * 24,
        "context_end_utc": [timestamp - pd.Timedelta(hours=24)] * 24,
        "target_timestamp_utc": [timestamp + pd.Timedelta(hours=h) for h in range(1, 25)],
        "horizon_hours": range(1, 25), "actual_demand_mw": range(24), "predicted_demand_mw": range(24),
    })


def test_adapter_retains_predictions_and_proves_causal_as_of_time() -> None:
    result = canonicalize_candidate(_candidate(), model_id="xgb", candidate_id="candidate")
    assert result["demand_unit"].eq("MW").all()
    assert result["feature_as_of_utc"].max() < result["forecast_origin_utc"].min()


def test_common_comparison_uses_exact_shared_curve_horizons() -> None:
    a = canonicalize_candidate(_candidate(), model_id="a", candidate_id="1")
    b = canonicalize_candidate(_candidate(), model_id="b", candidate_id="1").iloc[:-1]
    # The incomplete B curve is rejected before it can create a misleading
    # common-origin leaderboard.
    with pytest.raises(ForecastCurveContractError, match="exactly horizons"):
        strict_common_curve_rows(pd.concat([a, b]), model_ids=("a", "b"))
