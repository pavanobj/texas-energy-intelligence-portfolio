from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest

from texas_energy.forecast.catalog import ModelCatalogError, load_catalog, load_serving_model, pending_catalog


def _catalog() -> dict[str, object]:
    return {"schema_version": "1.0", "models": [{
        "model_id": "seasonal_naive_24h", "forecast_horizon_hours": 24,
        "operational_status": "baseline_inference_implemented", "default_selection_status": "selected_default",
    }]}


def test_catalog_loads_only_24h_contract_and_baseline_is_loadable(tmp_path) -> None:
    path = tmp_path / "catalog.json"; path.write_text(json.dumps(_catalog()), encoding="utf-8")
    model = load_serving_model(load_catalog(path))
    result = model.forecast(origin_timestamp_utc=datetime(2025, 1, 1, tzinfo=timezone.utc), demand_at_origin_mw=40_000)
    assert result["target_timestamp_utc"].startswith("2025-01-02T00:00:00")


def test_learned_artifact_is_not_claimed_serving_ready(tmp_path) -> None:
    catalog = _catalog(); catalog["models"].append({
        "model_id": "xgb", "forecast_horizon_hours": 24,
        "operational_status": "historical_evaluation_only", "default_selection_status": "evaluated_not_selected",
    })
    path = tmp_path / "catalog.json"; path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ModelCatalogError, match="not yet operationally"):
        load_serving_model(load_catalog(path), model_id="xgb")


def test_catalog_rejects_wrong_horizon(tmp_path) -> None:
    catalog = _catalog(); catalog["models"][0]["forecast_horizon_hours"] = 1  # type: ignore[index]
    path = tmp_path / "catalog.json"; path.write_text(json.dumps(catalog), encoding="utf-8")
    with pytest.raises(ModelCatalogError, match="24-hour"):
        load_catalog(path)


def test_pending_catalog_keeps_baseline_as_only_honest_default(tmp_path) -> None:
    catalog = pending_catalog(candidate_artifacts={"weather_aware_xgboost_curve": "/local/candidate"})
    path = tmp_path / "catalog.json"; path.write_text(json.dumps(catalog), encoding="utf-8")
    model = load_serving_model(load_catalog(path))
    assert model.model_id == "seasonal_naive_24h"
    assert catalog["models"][1]["evaluation_status"] == "pending_full_archive_evaluation"
