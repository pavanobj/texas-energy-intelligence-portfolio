"""Deterministic read-only tools over published Texas Energy dashboard JSON.

The caller provides an already validated, fixed allowlist of dashboard documents.
These tools never call AWS, run forecasts, or accept paths, prefixes, or keys.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Callable, Mapping


class DashboardToolError(ValueError):
    """Published dashboard data are missing or do not meet the tool contract."""


_DOCUMENTS = {
    "prospective-forecast.json",
    "prospective-evaluation-manifest.json",
    "rolling-prospective-evaluation.json",
    "rolling-historical-replay.json",
}
_MODEL_IDS = {"weather_aware_xgboost_curve", "weather_aware_bilstm_curve", "amazon/chronos-2"}
_CITIES = {"austin", "dallas", "el_paso", "houston", "san_antonio"}


def _finite(value: object, name: str) -> float:
    if isinstance(value, bool):
        raise DashboardToolError(f"{name} must be finite.")
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise DashboardToolError(f"{name} must be numeric.") from exc
    if not math.isfinite(result):
        raise DashboardToolError(f"{name} must be finite.")
    return result


@dataclass(frozen=True)
class DashboardDataStore:
    """Validated in-memory projection of the four canonical dashboard objects."""

    documents: Mapping[str, Mapping[str, Any]]
    sha256: Mapping[str, str]

    def __post_init__(self) -> None:
        if set(self.documents) != _DOCUMENTS or set(self.sha256) != _DOCUMENTS:
            raise DashboardToolError("The approved dashboard evidence set is incomplete.")
        if any(not isinstance(value, Mapping) for value in self.documents.values()):
            raise DashboardToolError("A dashboard evidence document has an invalid schema.")
        if any(not isinstance(value, str) or len(value) != 64 for value in self.sha256.values()):
            raise DashboardToolError("A dashboard evidence checksum is invalid.")

    def document(self, name: str) -> Mapping[str, Any]:
        return self.documents[name]

    def evidence(self, *names: str) -> list[dict[str, str]]:
        return [
            {"dashboard_data": f"dashboard/v1/data/{name}", "sha256": self.sha256[name]}
            for name in names
        ]


def get_latest_prospective_forecast(store: DashboardDataStore) -> dict[str, Any]:
    """Return all three genuine 24-hour published prospective forecast curves."""
    document = store.document("prospective-forecast.json")
    if document.get("forecast_generation_type") != "prospective_live_inference":
        raise DashboardToolError("Latest dashboard forecast is not a prospective inference issue.")
    models = document.get("models")
    if not isinstance(models, list) or {row.get("model_id") for row in models if isinstance(row, Mapping)} != _MODEL_IDS:
        raise DashboardToolError("Latest dashboard forecast lacks the three required model curves.")
    result_models = []
    for model in models:
        predictions = model.get("predictions")
        if not isinstance(predictions, list) or len(predictions) != 24:
            raise DashboardToolError("A published model curve does not contain 24 predictions.")
        records = []
        for row in predictions:
            if not isinstance(row, Mapping):
                raise DashboardToolError("A published forecast row is invalid.")
            records.append({
                "horizon_hours": row.get("horizon_hours"),
                "target_timestamp_utc": row.get("target_timestamp_utc"),
                "predicted_demand_mw": _finite(row.get("predicted_demand_mw"), "predicted_demand_mw"),
                "actual_demand_mw": row.get("actual_demand_mw"),
            })
        peak = max(records, key=lambda row: row["predicted_demand_mw"])
        result_models.append({
            "model_id": model.get("model_id"),
            "model_label": model.get("model_label"),
            "predicted_peak_demand_mw": peak["predicted_demand_mw"],
            "predicted_peak_target_utc": peak["target_timestamp_utc"],
            "metrics": model.get("metrics"),
            "predictions": records,
        })
    execution = document.get("execution")
    if not isinstance(execution, Mapping):
        execution = {}
    return {
        "tool": "get_latest_prospective_forecast",
        "status": document.get("status"),
        "forecast_generation_type": "prospective_live_inference",
        "issue_id": document.get("issue_id"),
        "forecast_issue_utc": document.get("forecast_origin_utc"),
        "forecast_origin_utc": document.get("forecast_origin_utc"),
        "model_execution_completed_at_utc": execution.get("model_execution_completed_at_utc"),
        "dashboard_published_at_utc": execution.get("dashboard_published_at_utc"),
        "execution": execution,
        "actual_coverage": document.get("actual_coverage"),
        "models": result_models,
        "evidence": store.evidence("prospective-forecast.json", "prospective-evaluation-manifest.json"),
    }


def get_rolling_prospective_metrics(store: DashboardDataStore, *, window_days: int) -> dict[str, Any]:
    """Return published prospective metrics where genuine Gold actuals exist."""
    if window_days not in {7, 14}:
        raise DashboardToolError("window_days must be 7 or 14.")
    document = store.document("rolling-prospective-evaluation.json")
    metrics = document.get("metrics", {}).get(f"{window_days}_day")
    if not isinstance(metrics, Mapping):
        raise DashboardToolError("Published prospective metrics are unavailable for the requested window.")
    return {
        "tool": "get_rolling_prospective_metrics",
        "status": document.get("status"),
        "historical_not_live": False,
        "evaluation_window_days": window_days,
        "issue_ids": document.get("issues"),
        "metrics": metrics,
        "freshness_utc": document.get("generated_at_utc"),
        "missing_data_rule": "Metrics are null or pending when genuine matching observations are unavailable; missing observations are never zero.",
        "evidence": store.evidence("rolling-prospective-evaluation.json"),
    }


def get_historical_backtest_metrics(store: DashboardDataStore, *, window_days: int) -> dict[str, Any]:
    """Return published historical replay metrics, explicitly labeled as backtests."""
    if window_days not in {7, 14}:
        raise DashboardToolError("window_days must be 7 or 14.")
    document = store.document("rolling-historical-replay.json")
    metrics = document.get("metrics", {}).get(f"{window_days}_day")
    if not isinstance(metrics, Mapping):
        raise DashboardToolError("Historical backtest metrics are unavailable for the requested window.")
    return {
        "tool": "get_historical_backtest_metrics",
        "status": "historical_rolling_backtest",
        "historical_not_live": True,
        "evaluation_window_days": window_days,
        "issue_start_utc": document.get("issue_start_utc"),
        "issue_end_utc": document.get("issue_end_utc"),
        "issue_count": document.get("issue_count"),
        "metrics": metrics,
        "evidence": store.evidence("rolling-historical-replay.json"),
    }


def get_weather_comparison(store: DashboardDataStore, *, city: str, scope: str) -> dict[str, Any]:
    """Return issued weather and only genuinely available observations for one city."""
    if city not in _CITIES:
        raise DashboardToolError("city is not in the approved five-city weather set.")
    if scope not in {"latest_prospective", "historical_backtest"}:
        raise DashboardToolError("scope must be latest_prospective or historical_backtest.")
    if scope == "latest_prospective":
        document = store.document("rolling-prospective-evaluation.json")
        latest = store.document("prospective-evaluation-manifest.json").get("latest_issue_id")
        records = [row for row in document.get("weather", {}).get("records", []) if isinstance(row, Mapping) and row.get("city") == city and row.get("issue_id") == latest]
        source = "rolling-prospective-evaluation.json"
        historical = False
    else:
        document = store.document("rolling-historical-replay.json")
        city_title = city.replace("_", " ").title()
        issue_ids = sorted({row.get("issue_id") for row in document.get("weather", {}).get("records", []) if isinstance(row, Mapping) and row.get("city") == city_title})
        latest = issue_ids[-1] if issue_ids else None
        records = [row for row in document.get("weather", {}).get("records", []) if isinstance(row, Mapping) and row.get("city") == city_title and row.get("issue_id") == latest]
        source = "rolling-historical-replay.json"
        historical = True
    if not records:
        raise DashboardToolError("No published weather records match the requested city and scope.")
    observed = sum(1 for row in records if row.get("observed_temperature_2m_c") is not None)
    return {
        "tool": "get_weather_comparison",
        "scope": scope,
        "historical_not_live": historical,
        "city": city,
        "issue_id": latest,
        "observation_count": observed,
        "missing_observation_count": len(records) - observed,
        "records": records,
        "evidence": store.evidence(source),
    }


DASHBOARD_TOOL_REGISTRY: dict[str, Callable[..., dict[str, Any]]] = {
    "get_latest_prospective_forecast": get_latest_prospective_forecast,
    "get_rolling_prospective_metrics": get_rolling_prospective_metrics,
    "get_historical_backtest_metrics": get_historical_backtest_metrics,
    "get_weather_comparison": get_weather_comparison,
}
