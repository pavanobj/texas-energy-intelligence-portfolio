"""Strict, reproducible comparison utilities for causal forecast candidates."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

import numpy as np
import pandas as pd


HORIZON_HOURS = 24
UNIFIED_PREDICTION_COLUMNS = (
    "model_id", "candidate_id", "origin_timestamp_utc", "target_timestamp_utc",
    "horizon_hours", "actual_demand_mw", "predicted_demand_mw",
    "baseline_predicted_demand_mw", "evaluation_split", "generation_type",
)


class UnifiedForecastError(ValueError):
    """A candidate cannot safely participate in a fair evaluation."""


@dataclass(frozen=True)
class Metrics:
    count: int
    mae_mw: float
    rmse_mw: float
    mape_pct: float
    smape_pct: float
    mean_error_mw: float
    median_absolute_error_mw: float
    p90_absolute_error_mw: float
    p95_absolute_error_mw: float
    maximum_absolute_error_mw: float
    mape_count: int

    def as_dict(self) -> dict[str, float | int]:
        return self.__dict__.copy()


def validate_predictions(frame: pd.DataFrame, *, gold_actuals: pd.Series | None = None) -> pd.DataFrame:
    """Validate schema, UTC horizon, finite MW values, and target truth."""

    missing = set(UNIFIED_PREDICTION_COLUMNS).difference(frame.columns)
    if missing:
        raise UnifiedForecastError(f"Prediction frame lacks columns: {sorted(missing)}")
    checked = frame.loc[:, list(UNIFIED_PREDICTION_COLUMNS)].copy()
    for name in ("origin_timestamp_utc", "target_timestamp_utc"):
        checked[name] = pd.to_datetime(checked[name], utc=True, errors="coerce")
    if checked[["origin_timestamp_utc", "target_timestamp_utc"]].isna().any().any():
        raise UnifiedForecastError("Prediction timestamps must be UTC-parseable.")
    if not (checked["target_timestamp_utc"] - checked["origin_timestamp_utc"]).eq(pd.Timedelta(hours=HORIZON_HOURS)).all():
        raise UnifiedForecastError("Every prediction must use the T+24 forecast contract.")
    if not checked["horizon_hours"].eq(HORIZON_HOURS).all():
        raise UnifiedForecastError("Every prediction must declare horizon_hours=24.")
    numeric = ["actual_demand_mw", "predicted_demand_mw", "baseline_predicted_demand_mw"]
    checked[numeric] = checked[numeric].apply(pd.to_numeric, errors="coerce")
    if not np.isfinite(checked[numeric].to_numpy(dtype=float)).all():
        raise UnifiedForecastError("Prediction and demand values must be finite MW values.")
    duplicate_key = ["model_id", "origin_timestamp_utc", "target_timestamp_utc"]
    if checked.duplicated(duplicate_key).any():
        raise UnifiedForecastError("Duplicate model/origin/target predictions are not allowed.")
    if gold_actuals is not None:
        reference = gold_actuals.copy()
        reference.index = pd.to_datetime(reference.index, utc=True)
        expected = checked["target_timestamp_utc"].map(reference)
        if expected.isna().any() or not np.allclose(expected.to_numpy(float), checked["actual_demand_mw"].to_numpy(float)):
            raise UnifiedForecastError("Candidate actual targets do not exactly match accepted Gold demand.")
    return checked.sort_values(["model_id", "target_timestamp_utc"]).reset_index(drop=True)


def metrics(actual: pd.Series | np.ndarray, predicted: pd.Series | np.ndarray) -> Metrics:
    actual_values = np.asarray(actual, dtype=float)
    predicted_values = np.asarray(predicted, dtype=float)
    if actual_values.shape != predicted_values.shape or actual_values.size == 0:
        raise UnifiedForecastError("Metrics require equally sized, nonempty arrays.")
    if not np.isfinite(actual_values).all() or not np.isfinite(predicted_values).all():
        raise UnifiedForecastError("Metrics require finite values.")
    error = predicted_values - actual_values
    absolute = np.abs(error)
    valid_mape = np.abs(actual_values) > 1e-9
    smape_denom = np.abs(actual_values) + np.abs(predicted_values)
    return Metrics(
        count=int(actual_values.size), mae_mw=float(absolute.mean()), rmse_mw=float(sqrt(np.square(error).mean())),
        mape_pct=float((absolute[valid_mape] / np.abs(actual_values[valid_mape])).mean() * 100) if valid_mape.any() else float("nan"),
        smape_pct=float((2 * absolute[smape_denom > 1e-9] / smape_denom[smape_denom > 1e-9]).mean() * 100)
        if (smape_denom > 1e-9).any() else float("nan"),
        mean_error_mw=float(error.mean()), median_absolute_error_mw=float(np.median(absolute)),
        p90_absolute_error_mw=float(np.quantile(absolute, 0.90)), p95_absolute_error_mw=float(np.quantile(absolute, 0.95)),
        maximum_absolute_error_mw=float(absolute.max()), mape_count=int(valid_mape.sum()),
    )


def strict_common_origins(frame: pd.DataFrame, *, model_ids: tuple[str, ...]) -> pd.DataFrame:
    """Return predictions only on exact origin/target pairs shared by all models."""

    checked = validate_predictions(frame)
    key = ["origin_timestamp_utc", "target_timestamp_utc", "evaluation_split"]
    present = checked.loc[checked["model_id"].isin(model_ids)].groupby(key, as_index=False)["model_id"].nunique()
    common = present.loc[present["model_id"].eq(len(model_ids)), key]
    result = checked.merge(common, on=key, how="inner")
    return result.loc[result["model_id"].isin(model_ids)].sort_values(["evaluation_split", "target_timestamp_utc", "model_id"]).reset_index(drop=True)


def baseline_rows(common: pd.DataFrame) -> pd.DataFrame:
    """Derive one verified seasonal-naive row for each strict common origin."""

    key = ["origin_timestamp_utc", "target_timestamp_utc", "evaluation_split"]
    agreement = common.groupby(key, as_index=False).agg(
        actual=("actual_demand_mw", "nunique"), baseline=("baseline_predicted_demand_mw", "nunique")
    )
    if not agreement[["actual", "baseline"]].eq(1).all().all():
        raise UnifiedForecastError("Candidates disagree on actual or seasonal-naive values for a common origin.")
    rows = common.drop_duplicates(key).loc[:, key + ["actual_demand_mw", "baseline_predicted_demand_mw"]].copy()
    rows["model_id"] = "seasonal_naive_24h"
    rows["candidate_id"] = "derived-strict-common-origin"
    rows["horizon_hours"] = HORIZON_HOURS
    rows["predicted_demand_mw"] = rows["baseline_predicted_demand_mw"]
    rows["generation_type"] = "historical_backtest"
    return rows.loc[:, list(UNIFIED_PREDICTION_COLUMNS)]


def leaderboard(frame: pd.DataFrame) -> list[dict[str, object]]:
    """Calculate deterministic metrics per split/model from one evaluation cohort."""

    result: list[dict[str, object]] = []
    for (split, model_id), group in frame.groupby(["evaluation_split", "model_id"], sort=True):
        row: dict[str, object] = {"evaluation_split": split, "model_id": model_id}
        row.update(metrics(group["actual_demand_mw"], group["predicted_demand_mw"]).as_dict())
        result.append(row)
    return result
