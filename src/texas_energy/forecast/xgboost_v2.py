"""Causal 24-hour-ahead XGBoost V2 dataset preparation and evaluation.

The Gold feature table contains historical observed weather as lineage-rich
training data.  This module intentionally does *not* use those columns: a
weather observation at a 24-hour target timestamp is not available at the
forecast origin without a separately versioned forecast-weather contract.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


HORIZON_HOURS = 24
CAUSAL_DEMAND_FEATURES = (
    "demand_lag_1h_mwh",
    "demand_lag_24h_mwh",
    "demand_lag_168h_mwh",
    "demand_trailing_mean_24h_mwh",
    "demand_trailing_mean_168h_mwh",
)
TARGET_CALENDAR_FEATURES = (
    "target_utc_hour",
    "target_utc_day_of_week_sun1",
    "target_utc_month",
    "target_is_weekend_utc",
)
FEATURE_COLUMNS = CAUSAL_DEMAND_FEATURES + TARGET_CALENDAR_FEATURES
TARGET_COLUMN = "target_average_demand_mw"


class XGBoostV2ContractError(ValueError):
    """Gold data cannot form a safe XGBoost V2 forecast example."""


@dataclass(frozen=True)
class PreparedXGBoostV2Data:
    """Leakage-safe examples and the reasons ineligible Gold rows were removed."""

    examples: pd.DataFrame
    excluded_no_target: int
    excluded_incomplete_causal_context: int


def build_supervised_examples(gold: pd.DataFrame) -> PreparedXGBoostV2Data:
    """Create examples at origin ``t`` for the actual load at ``t + 24h``.

    The Gold lags and rolling means are already computed from observations
    before their row timestamp.  Null values at segment boundaries are kept
    as an explicit ineligibility signal, so an example can never bridge an
    ERCOT source-hour gap.  Target-calendar fields are deterministic from the
    known future timestamp; observed target demand and all weather columns are
    deliberately excluded from the feature list.
    """

    required = {"period_utc", "observed_average_demand_mw", *CAUSAL_DEMAND_FEATURES}
    missing = sorted(required.difference(gold.columns))
    if missing:
        raise XGBoostV2ContractError(f"Gold input lacks required columns: {', '.join(missing)}.")

    frame = gold.loc[:, ["period_utc", "observed_average_demand_mw", *CAUSAL_DEMAND_FEATURES]].copy()
    frame["period_utc"] = pd.to_datetime(frame["period_utc"], utc=True, errors="coerce")
    if frame["period_utc"].isna().any() or not frame["period_utc"].dt.floor("h").eq(frame["period_utc"]).all():
        raise XGBoostV2ContractError("Gold period_utc values must be UTC-aligned hourly timestamps.")
    if frame["period_utc"].duplicated().any():
        raise XGBoostV2ContractError("Gold period_utc values must be unique.")
    if frame["observed_average_demand_mw"].isna().any():
        raise XGBoostV2ContractError("Gold observed demand cannot be null.")

    frame = frame.sort_values("period_utc").reset_index(drop=True)
    source_values = frame.set_index("period_utc")["observed_average_demand_mw"]
    frame["origin_utc"] = frame["period_utc"]
    frame["target_utc"] = frame["origin_utc"] + pd.Timedelta(hours=HORIZON_HOURS)
    frame[TARGET_COLUMN] = frame["target_utc"].map(source_values)
    no_target = frame[TARGET_COLUMN].isna()
    incomplete_context = frame.loc[:, list(CAUSAL_DEMAND_FEATURES)].isna().any(axis=1)

    target = frame["target_utc"]
    frame["target_utc_hour"] = target.dt.hour.astype("int8")
    # Gold uses Sunday=1 through Saturday=7; retain that notebook-compatible convention.
    frame["target_utc_day_of_week_sun1"] = ((target.dt.dayofweek + 1) % 7 + 1).astype("int8")
    frame["target_utc_month"] = target.dt.month.astype("int8")
    frame["target_is_weekend_utc"] = (target.dt.dayofweek >= 5).astype("int8")

    examples = frame.loc[~no_target & ~incomplete_context, [
        "origin_utc", "target_utc", "observed_average_demand_mw", TARGET_COLUMN, *FEATURE_COLUMNS
    ]].copy()
    return PreparedXGBoostV2Data(
        examples=examples.reset_index(drop=True),
        excluded_no_target=int(no_target.sum()),
        excluded_incomplete_causal_context=int((~no_target & incomplete_context).sum()),
    )


def split_examples(examples: pd.DataFrame) -> dict[str, pd.DataFrame]:
    """Split examples by target timestamp, preserving the requested holdout."""

    target = pd.to_datetime(examples["target_utc"], utc=True)
    result = {
        "train": examples.loc[(target >= "2020-01-01") & (target < "2024-01-01")].copy(),
        "validation": examples.loc[(target >= "2024-01-01") & (target < "2025-01-01")].copy(),
        "test": examples.loc[(target >= "2025-01-01") & (target < "2026-01-01")].copy(),
    }
    if any(part.empty for part in result.values()):
        raise XGBoostV2ContractError("Every chronological XGBoost V2 split must contain eligible examples.")
    return result


def fit_xgboost_v2(
    train: pd.DataFrame, validation: pd.DataFrame, *, n_jobs: int = 4, n_estimators: int = 1500
) -> Any:
    """Fit the notebook's V2 XGBRegressor configuration with early stopping."""

    try:
        from xgboost import XGBRegressor
    except ImportError:
        raise RuntimeError("Install the forecast optional dependency group to train XGBoost V2.") from None
    model = XGBRegressor(
        objective="reg:squarederror",
        n_estimators=n_estimators,
        learning_rate=0.03,
        max_depth=8,
        min_child_weight=5,
        subsample=0.8,
        colsample_bytree=0.8,
        reg_alpha=0.0,
        reg_lambda=1.0,
        random_state=42,
        tree_method="hist",
        eval_metric="rmse",
        early_stopping_rounds=75,
        n_jobs=n_jobs,
    )
    model.fit(
        train.loc[:, list(FEATURE_COLUMNS)],
        train[TARGET_COLUMN],
        eval_set=[(validation.loc[:, list(FEATURE_COLUMNS)], validation[TARGET_COLUMN])],
        verbose=False,
    )
    return model


def evaluate(actual: pd.Series | np.ndarray, prediction: pd.Series | np.ndarray) -> dict[str, float | int]:
    """Return explicit MW metrics plus percentage metrics on nonzero targets."""

    actual_values = np.asarray(actual, dtype=float)
    predicted_values = np.asarray(prediction, dtype=float)
    if actual_values.shape != predicted_values.shape or actual_values.size == 0:
        raise XGBoostV2ContractError("Metrics require equally sized, nonempty prediction and target arrays.")
    error = predicted_values - actual_values
    nonzero = actual_values != 0
    denominator = np.abs(actual_values[nonzero])
    smape_denominator = np.abs(actual_values) + np.abs(predicted_values)
    return {
        "count": int(actual_values.size),
        "mae_mw": float(np.mean(np.abs(error))),
        "rmse_mw": float(np.sqrt(np.mean(np.square(error)))),
        "bias_mw": float(np.mean(error)),
        "mape_pct": float(np.mean(np.abs(error[nonzero]) / denominator) * 100) if nonzero.any() else float("nan"),
        "smape_pct": float(np.mean(2 * np.abs(error[smape_denominator != 0]) / smape_denominator[smape_denominator != 0]) * 100)
        if (smape_denominator != 0).any() else float("nan"),
        "mape_count": int(nonzero.sum()),
    }
