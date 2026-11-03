"""Trusted, deterministic analysis over published historical forecast artifacts.

These functions are intentionally framework-free: an application or a future
agent can call them directly, but none can run commands, invoke a model, or
reach a network service.  Their inputs select only from verified local files.
"""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

import pandas as pd


class AnalyticsToolError(ValueError):
    """A request is invalid or cannot be grounded in a trusted artifact."""


MODEL_COLUMNS = {
    "xgboost_curve_weather_aware": "xgboost_weather_aware_predicted_demand_mw",
    "bilstm_curve_weather_aware": "bilstm_weather_aware_predicted_demand_mw",
    "amazon/chronos-2": "chronos2_predicted_demand_mw",
}
MODEL_LABELS = {
    "xgboost_curve_weather_aware": "Weather-aware XGBoost",
    "bilstm_curve_weather_aware": "Weather-aware Bi-LSTM",
    "amazon/chronos-2": "Chronos-2 zero-shot",
    "xgboost_curve_demand_only": "Demand-only XGBoost",
    "bilstm_curve_demand_only": "Demand-only Bi-LSTM",
    "seasonal_naive_context_end": "Seasonal-naive baseline",
}
METRICS = {"mae_mw", "rmse_mw", "mape_pct", "smape_pct"}


def _utc(value: Any) -> str:
    timestamp = pd.Timestamp(value)
    if timestamp.tzinfo is None:
        raise AnalyticsToolError("Artifact timestamp must be timezone-aware UTC.")
    return timestamp.tz_convert("UTC").isoformat()


def _finite(value: Any, name: str) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise AnalyticsToolError(f"{name} must be numeric.") from exc
    if not math.isfinite(result):
        raise AnalyticsToolError(f"{name} must be finite.")
    return result


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


@dataclass(frozen=True)
class AnalyticsArtifactStore:
    """Read-only gateway to one verified final-closeout artifact root."""

    closeout_root: Path

    def __post_init__(self) -> None:
        root = self.closeout_root.expanduser().resolve()
        object.__setattr__(self, "closeout_root", root)
        required = ("historical_demo/heldout_2025-07-01_06z_24h.parquet", "comparison/leaderboard.json", "comparison/per_horizon_leaderboard.json", "model_catalog.json")
        missing = [item for item in required if not (root / item).is_file()]
        if missing:
            raise AnalyticsToolError(f"Trusted forecast artifacts are missing: {missing}")

    def evidence(self, relative_path: str) -> dict[str, str]:
        path = self.closeout_root / relative_path
        if not path.is_file():
            raise AnalyticsToolError("Requested evidence artifact is unavailable.")
        return {"artifact": relative_path, "sha256": _sha256(path)}

    def demo(self) -> pd.DataFrame:
        frame = pd.read_parquet(self.closeout_root / "historical_demo/heldout_2025-07-01_06z_24h.parquet").copy()
        needed = {"forecast_origin_utc", "target_timestamp_utc", "actual_demand_mw", *MODEL_COLUMNS.values()}
        if missing := needed.difference(frame.columns):
            raise AnalyticsToolError(f"Historical demo lacks required columns: {sorted(missing)}")
        frame["forecast_origin_utc"] = pd.to_datetime(frame["forecast_origin_utc"], utc=True, errors="raise")
        frame["target_timestamp_utc"] = pd.to_datetime(frame["target_timestamp_utc"], utc=True, errors="raise")
        frame = frame.sort_values("target_timestamp_utc").reset_index(drop=True)
        origin = frame["forecast_origin_utc"].iloc[0]
        if len(frame) != 24 or frame["target_timestamp_utc"].duplicated().any():
            raise AnalyticsToolError("Historical demo must contain exactly 24 unique targets.")
        for horizon, row in enumerate(frame.itertuples(), start=1):
            if row.target_timestamp_utc != origin + pd.Timedelta(hours=horizon):
                raise AnalyticsToolError("Historical demo target timestamps are not 24 consecutive UTC horizons.")
        return frame

    def leaderboard(self, *, per_horizon: bool = False) -> list[dict[str, Any]]:
        name = "comparison/per_horizon_leaderboard.json" if per_horizon else "comparison/leaderboard.json"
        result = json.loads((self.closeout_root / name).read_text(encoding="utf-8"))
        if not isinstance(result, list):
            raise AnalyticsToolError("Performance artifact has an invalid schema.")
        return result

    def catalog(self) -> dict[str, Any]:
        result = json.loads((self.closeout_root / "model_catalog.json").read_text(encoding="utf-8"))
        if not isinstance(result, dict):
            raise AnalyticsToolError("Model catalog has an invalid schema.")
        return result


def _model_column(model_id: str) -> str:
    try:
        return MODEL_COLUMNS[model_id]
    except KeyError as exc:
        raise AnalyticsToolError(f"Unknown replay model_id: {model_id}") from exc


def _summary_records(frame: pd.DataFrame, column: str) -> list[dict[str, Any]]:
    return [{"horizon_hours": index, "target_timestamp_utc": _utc(row.target_timestamp_utc), "predicted_demand_mw": _finite(getattr(row, column), "predicted_demand_mw"), "actual_demand_mw": _finite(row.actual_demand_mw, "actual_demand_mw"), "unit": "MW"} for index, row in enumerate(frame.itertuples(), start=1)]


def get_forecast_summary(store: AnalyticsArtifactStore, *, model_id: str = "bilstm_curve_weather_aware") -> dict[str, Any]:
    """Summarize one genuine 24-hour historical replay without inference."""
    frame = store.demo(); column = _model_column(model_id); records = _summary_records(frame, column)
    values = [row["predicted_demand_mw"] for row in records]; peak_index = max(range(len(values)), key=values.__getitem__); trough_index = min(range(len(values)), key=values.__getitem__)
    return {"tool": "get_forecast_summary", "status": "historical_replay", "historical_not_live": True, "model_id": model_id, "model_label": MODEL_LABELS[model_id], "forecast_origin_utc": _utc(frame.forecast_origin_utc.iloc[0]), "unit": "MW", "target_count": 24, "predicted_peak_demand_mw": values[peak_index], "predicted_peak_timestamp_utc": records[peak_index]["target_timestamp_utc"], "predicted_minimum_demand_mw": values[trough_index], "predicted_minimum_timestamp_utc": records[trough_index]["target_timestamp_utc"], "average_predicted_demand_mw": sum(values) / len(values), "sum_hourly_demand_mwh": sum(values), "records": records, "evidence": [store.evidence("historical_demo/heldout_2025-07-01_06z_24h.parquet")]}


def compare_model_forecasts(store: AnalyticsArtifactStore, *, left_model_id: str, right_model_id: str) -> dict[str, Any]:
    """Compare saved curve values on their shared, exact 24 replay targets."""
    frame = store.demo(); left_column = _model_column(left_model_id); right_column = _model_column(right_model_id)
    rows: list[dict[str, Any]] = []
    for horizon, row in enumerate(frame.itertuples(), start=1):
        left = _finite(getattr(row, left_column), "left prediction"); right = _finite(getattr(row, right_column), "right prediction")
        rows.append({"horizon_hours": horizon, "target_timestamp_utc": _utc(row.target_timestamp_utc), "left_predicted_demand_mw": left, "right_predicted_demand_mw": right, "signed_difference_mw": left - right, "absolute_difference_mw": abs(left - right), "unit": "MW"})
    max_row = max(rows, key=lambda item: item["absolute_difference_mw"])
    return {"tool": "compare_model_forecasts", "status": "historical_replay", "historical_not_live": True, "left_model_id": left_model_id, "right_model_id": right_model_id, "forecast_origin_utc": _utc(frame.forecast_origin_utc.iloc[0]), "target_count": 24, "mean_absolute_disagreement_mw": sum(item["absolute_difference_mw"] for item in rows) / len(rows), "maximum_disagreement": max_row, "interpretation": "Model disagreement is not a forecasting-error measure.", "records": rows, "evidence": [store.evidence("historical_demo/heldout_2025-07-01_06z_24h.parquet")]}


def analyze_forecast_errors(store: AnalyticsArtifactStore, *, model_id: str) -> dict[str, Any]:
    """Calculate post-outcome errors only where the replay stores actual demand."""
    summary = get_forecast_summary(store, model_id=model_id); errors = [row["predicted_demand_mw"] - row["actual_demand_mw"] for row in summary["records"]]
    actual = [row["actual_demand_mw"] for row in summary["records"]]; peak_index = max(range(len(actual)), key=actual.__getitem__)
    return {"tool": "analyze_forecast_errors", "status": "post_outcome_historical_evaluation", "available_at_forecast_issuance": False, "model_id": model_id, "unit": "MW", "target_count": len(errors), "mae_mw": sum(abs(error) for error in errors) / len(errors), "rmse_mw": math.sqrt(sum(error * error for error in errors) / len(errors)), "bias_mw": sum(errors) / len(errors), "peak_actual_timestamp_utc": summary["records"][peak_index]["target_timestamp_utc"], "peak_actual_demand_mw": actual[peak_index], "peak_hour_error_mw": errors[peak_index], "evidence": summary["evidence"]}


def get_model_performance(store: AnalyticsArtifactStore, *, split: str, metric: str = "mae_mw", horizon_hours: int | None = None) -> dict[str, Any]:
    """Rank genuine validation *or* test metrics, never a mixed cohort."""
    if split not in {"validation", "test"}: raise AnalyticsToolError("split must be validation or test.")
    if metric not in METRICS: raise AnalyticsToolError(f"metric must be one of {sorted(METRICS)}.")
    rows = store.leaderboard(per_horizon=horizon_hours is not None)
    rows = [dict(row) for row in rows if row.get("evaluation_split") == split and (horizon_hours is None or row.get("horizon_hours") == horizon_hours)]
    if not rows: raise AnalyticsToolError("No trusted performance rows match the requested split/horizon.")
    if any(metric not in row for row in rows): raise AnalyticsToolError("Requested metric is unavailable in the trusted performance artifact.")
    ordered = sorted(rows, key=lambda row: _finite(row[metric], metric))
    return {"tool": "get_model_performance", "status": "historical_evaluation", "evaluation_split": split, "metric": metric, "horizon_hours": horizon_hours, "rankings": [{"rank": index, "model_id": row["model_id"], "model_label": MODEL_LABELS.get(row["model_id"], row["model_id"]), "metric_value": _finite(row[metric], metric), "metric_unit": "MW" if metric.endswith("_mw") else "percent", "count": row.get("count")} for index, row in enumerate(ordered, start=1)], "evidence": [store.evidence("comparison/per_horizon_leaderboard.json" if horizon_hours is not None else "comparison/leaderboard.json")]}


def analyze_demand_patterns(store: AnalyticsArtifactStore) -> dict[str, Any]:
    """Describe only the 24 actual demand values supplied by the replay."""
    frame = store.demo(); values = [_finite(value, "actual_demand_mw") for value in frame.actual_demand_mw]; deltas = [values[index] - values[index - 1] for index in range(1, len(values))]
    return {"tool": "analyze_demand_patterns", "status": "short_historical_replay_only", "historical_not_live": True, "coverage_hours": len(values), "coverage_start_utc": _utc(frame.target_timestamp_utc.iloc[0]), "coverage_end_utc": _utc(frame.target_timestamp_utc.iloc[-1]), "unit": "MW", "peak_demand_mw": max(values), "trough_demand_mw": min(values), "largest_hourly_increase_mw": max(deltas), "largest_hourly_decrease_mw": min(deltas), "limitation": "This 24-hour replay is not a long-term demand study.", "evidence": [store.evidence("historical_demo/heldout_2025-07-01_06z_24h.parquet")]}


def get_weather_context(store: AnalyticsArtifactStore) -> dict[str, Any]:
    """Return provenance; decline numeric weather values absent from the demo."""
    frame = store.demo(); row = frame.iloc[0]
    return {"tool": "get_weather_context", "status": "metadata_only", "historical_not_live": True, "numeric_weather_available": False, "forecast_origin_utc": _utc(row.forecast_origin_utc), "weather_model_run_utc": _utc(row.weather_model_run_utc) if "weather_model_run_utc" in frame else None, "weather_available_at_utc": _utc(row.weather_available_at_utc) if "weather_available_at_utc" in frame else None, "message": "The trusted replay preserves weather provenance but not numerical city-weather values.", "evidence": [store.evidence("historical_demo/heldout_2025-07-01_06z_24h.parquet")]}


TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {"get_forecast_summary": get_forecast_summary, "compare_model_forecasts": compare_model_forecasts, "analyze_forecast_errors": analyze_forecast_errors, "get_model_performance": get_model_performance, "analyze_demand_patterns": analyze_demand_patterns, "get_weather_context": get_weather_context}


def tool_schemas() -> dict[str, dict[str, Any]]:
    """Small serializable discovery contract for a future private tool caller."""
    return {name: {"name": name, "input": {"closeout_root": "trusted local artifact root", "parameters": "named JSON-compatible fields only"}, "output": "JSON-serializable structured result with evidence", "network_or_model_execution": False} for name in TOOL_REGISTRY}
