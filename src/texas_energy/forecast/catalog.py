"""File-backed model catalog and safe baseline serving primitive."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


class ModelCatalogError(ValueError):
    pass


def pending_catalog(*, candidate_artifacts: dict[str, str]) -> dict[str, object]:
    """Create an honest local serving catalog before final model evaluation.

    The operational baseline is the only selected default.  Candidate entries
    deliberately carry no metrics and are not serving-eligible until their
    archived-weather evaluation and artifact-loading smoke tests complete.
    """

    models: list[dict[str, object]] = [{
        "model_id": "seasonal_naive_24h", "model_family": "seasonal_naive", "candidate_version": "v1",
        "artifact_path": None, "mlflow_run_id": None, "forecast_horizon_hours": 24,
        "supported_context_hours": 1, "required_features": ["demand_at_origin_mw"],
        "feature_availability_contract": "Uses only the observed origin demand.",
        "evaluation_status": "baseline_contract_verified", "operational_status": "baseline_inference_implemented",
        "serving_eligibility": True, "default_selection_status": "selected_default",
        "selection_reason": "Temporary conservative default until archived-weather candidates are evaluated on validation data.",
    }]
    families = {"weather_aware_xgboost_curve": "XGBoost", "weather_aware_bilstm_curve": "Bi-LSTM", "amazon/chronos-2": "Chronos-2"}
    for model_id, artifact_path in sorted(candidate_artifacts.items()):
        models.append({
            "model_id": model_id, "model_family": families.get(model_id, "unknown"), "candidate_version": "pending",
            "artifact_path": artifact_path, "mlflow_run_id": None, "forecast_horizon_hours": 24,
            "supported_context_hours": 2048 if model_id == "amazon/chronos-2" else 168,
            "required_features": ["historical_demand", "issued_weather_forecasts"] if model_id != "amazon/chronos-2" else ["historical_demand"],
            "feature_availability_contract": "Requires point-in-time-valid input verification before live serving.",
            "evaluation_status": "pending_full_archive_evaluation", "operational_status": "artifact_present_not_load_tested",
            "serving_eligibility": False, "default_selection_status": "evaluated_not_selected",
            "selection_reason": "Not eligible until chronological validation evaluation and local loader smoke test complete.",
        })
    return {"schema_version": "1.0", "catalog_status": "pending_full_archive_evaluation", "models": models}


def load_catalog(path: Path) -> dict[str, object]:
    catalog = json.loads(path.read_text(encoding="utf-8"))
    if catalog.get("schema_version") != "1.0" or not isinstance(catalog.get("models"), list):
        raise ModelCatalogError("Unsupported model catalog schema.")
    seen: set[str] = set()
    for item in catalog["models"]:
        if not isinstance(item, dict):
            raise ModelCatalogError("Catalog models must be objects.")
        model_id = item.get("model_id")
        if not isinstance(model_id, str) or not model_id or model_id in seen:
            raise ModelCatalogError("Catalog model_id values must be unique non-empty strings.")
        seen.add(model_id)
        if item.get("forecast_horizon_hours") != 24:
            raise ModelCatalogError("Only the approved 24-hour forecast contract is loadable.")
        status = item.get("operational_status")
        if status not in {"baseline_inference_implemented", "artifact_present_not_load_tested", "historical_evaluation_only", "local_load_tested"}:
            raise ModelCatalogError("Catalog operational_status is invalid or missing.")
    return catalog


def resolve_default(catalog: dict[str, object]) -> dict[str, object]:
    selected = [item for item in catalog["models"] if item.get("default_selection_status") == "selected_default"]
    if len(selected) != 1:
        raise ModelCatalogError("Catalog must contain exactly one selected default model.")
    return selected[0]


def catalog_model_status(catalog: dict[str, object], *, model_id: str) -> dict[str, object]:
    """Return model metadata without trying to import a heavy ML runtime."""

    for item in catalog.get("models", []):
        if item.get("model_id") == model_id:
            return dict(item)
    raise ModelCatalogError("Requested model is not present in the catalog.")


def load_serving_model(catalog: dict[str, object], *, model_id: str | None = None) -> "SeasonalNaive24h":
    """Load only a demonstrably operational local model.

    The baseline is intentionally the sole loadable model today.  Learned
    artifacts remain historical-evaluation-only until a separate, point-in-
    time input provider can supply their required demand/weather inputs.
    Returning an error is safer than claiming an artifact-only model can make
    a live forecast.
    """

    entry = resolve_default(catalog) if model_id is None else catalog_model_status(catalog, model_id=model_id)
    if entry.get("model_id") != "seasonal_naive_24h" or entry.get("operational_status") != "baseline_inference_implemented":
        raise ModelCatalogError("Requested learned model is not yet operationally load-tested for live serving.")
    return SeasonalNaive24h(model_id=str(entry["model_id"]))


@dataclass(frozen=True)
class SeasonalNaive24h:
    """Fully functional serving primitive; caller supplies the known origin load."""

    model_id: str = "seasonal_naive_24h"
    horizon_hours: int = 24

    def forecast(self, *, origin_timestamp_utc: datetime, demand_at_origin_mw: float) -> dict[str, object]:
        if origin_timestamp_utc.tzinfo is None or origin_timestamp_utc.utcoffset() != timedelta(0):
            raise ModelCatalogError("origin_timestamp_utc must be timezone-aware UTC.")
        if self.horizon_hours != 24:
            raise ModelCatalogError("Only the approved 24-hour baseline contract is supported.")
        if not isinstance(demand_at_origin_mw, (int, float)) or isinstance(demand_at_origin_mw, bool) or not math.isfinite(float(demand_at_origin_mw)):
            raise ModelCatalogError("demand_at_origin_mw must be a finite numeric MW value.")
        return {
            "model_id": self.model_id,
            "origin_timestamp_utc": origin_timestamp_utc.astimezone(timezone.utc).isoformat(),
            "target_timestamp_utc": (origin_timestamp_utc + timedelta(hours=24)).astimezone(timezone.utc).isoformat(),
            "horizon_hours": 24,
            "prediction_mw": float(demand_at_origin_mw),
            "generation_type": "live_inference",
        }
