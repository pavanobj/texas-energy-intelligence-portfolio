"""Preserved forecast contracts derived from the final model notebooks."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ForecastModelSpec:
    name: str
    horizon_hours: int
    context_hours: int
    target_column: str = "observed_average_demand_mw"


# Do not unify these horizons: the notebook targets are intentionally distinct.
MODEL_SPECS = {
    "xgboost_v2": ForecastModelSpec("xgboost_v2", horizon_hours=24, context_hours=168),
    "bilstm_v2": ForecastModelSpec("bilstm_v2", horizon_hours=1, context_hours=168),
    "chronos_2": ForecastModelSpec("chronos_2", horizon_hours=1, context_hours=2048),
    # The completed local artifacts are distinct T+24 benchmarks.  Keeping
    # their identities versioned prevents a legacy next-hour contract from
    # being silently reinterpreted by a future serving layer.
    "bilstm_v2_t24_benchmark": ForecastModelSpec(
        "bilstm_v2_t24_benchmark", horizon_hours=24, context_hours=168
    ),
    "chronos_2_t24_benchmark": ForecastModelSpec(
        "chronos_2_t24_benchmark", horizon_hours=24, context_hours=2048
    ),
}
