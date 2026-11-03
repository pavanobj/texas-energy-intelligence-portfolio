"""Tests for deterministic private analytics tools using local fixture artifacts."""
from __future__ import annotations

import json
from pathlib import Path

import pandas as pd
import pytest

from texas_energy.analytics.tools import (
    AnalyticsArtifactStore,
    AnalyticsToolError,
    analyze_demand_patterns,
    analyze_forecast_errors,
    compare_model_forecasts,
    get_forecast_summary,
    get_model_performance,
    get_weather_context,
    tool_schemas,
)


@pytest.fixture
def artifacts(tmp_path: Path) -> AnalyticsArtifactStore:
    demo = tmp_path / "historical_demo"; comparison = tmp_path / "comparison"
    demo.mkdir(); comparison.mkdir()
    origin = pd.Timestamp("2025-07-01T06:00:00Z")
    rows = []
    for horizon in range(1, 25):
        actual = 50_000.0 + horizon
        rows.append({"forecast_origin_utc": origin, "target_timestamp_utc": origin + pd.Timedelta(hours=horizon), "actual_demand_mw": actual, "xgboost_weather_aware_predicted_demand_mw": actual + 30.0, "bilstm_weather_aware_predicted_demand_mw": actual - 10.0, "chronos2_predicted_demand_mw": actual + 5.0, "weather_model_run_utc": origin - pd.Timedelta(hours=6), "weather_available_at_utc": origin})
    pd.DataFrame(rows).to_parquet(demo / "heldout_2025-07-01_06z_24h.parquet", index=False)
    leaderboard = [{"model_id": "bilstm_curve_weather_aware", "evaluation_split": "validation", "mae_mw": 10.0, "rmse_mw": 11.0, "mape_pct": 1.0, "smape_pct": 1.1, "count": 24}, {"model_id": "amazon/chronos-2", "evaluation_split": "validation", "mae_mw": 15.0, "rmse_mw": 16.0, "mape_pct": 1.5, "smape_pct": 1.6, "count": 24}, {"model_id": "bilstm_curve_weather_aware", "evaluation_split": "test", "mae_mw": 12.0, "rmse_mw": 13.0, "mape_pct": 1.2, "smape_pct": 1.3, "count": 24}]
    (comparison / "leaderboard.json").write_text(json.dumps(leaderboard), encoding="utf-8")
    per = [dict(leaderboard[0], horizon_hours=1), dict(leaderboard[1], horizon_hours=1)]
    (comparison / "per_horizon_leaderboard.json").write_text(json.dumps(per), encoding="utf-8")
    (tmp_path / "model_catalog.json").write_text(json.dumps({"schema_version": "1.0", "selection": {"selected_default": "bilstm_curve_weather_aware"}, "models": []}), encoding="utf-8")
    return AnalyticsArtifactStore(tmp_path)


def test_forecast_summary_preserves_utc_mw_and_exact_horizons(artifacts: AnalyticsArtifactStore) -> None:
    result = get_forecast_summary(artifacts)
    assert result["historical_not_live"] is True
    assert result["target_count"] == 24
    assert result["records"][0]["horizon_hours"] == 1
    assert result["records"][-1]["horizon_hours"] == 24
    assert result["records"][0]["unit"] == "MW"
    assert result["predicted_peak_demand_mw"] == 50_014.0


def test_comparison_uses_matching_targets_and_disagreement_is_not_error(artifacts: AnalyticsArtifactStore) -> None:
    result = compare_model_forecasts(artifacts, left_model_id="bilstm_curve_weather_aware", right_model_id="amazon/chronos-2")
    assert len(result["records"]) == 24
    assert result["maximum_disagreement"]["absolute_difference_mw"] == 15.0
    assert "not a forecasting-error" in result["interpretation"]


def test_error_analysis_is_explicitly_post_outcome(artifacts: AnalyticsArtifactStore) -> None:
    result = analyze_forecast_errors(artifacts, model_id="bilstm_curve_weather_aware")
    assert result["available_at_forecast_issuance"] is False
    assert result["mae_mw"] == 10.0
    assert result["bias_mw"] == -10.0


def test_performance_separates_validation_test_and_horizons(artifacts: AnalyticsArtifactStore) -> None:
    validation = get_model_performance(artifacts, split="validation")
    assert validation["rankings"][0]["model_id"] == "bilstm_curve_weather_aware"
    assert {item["count"] for item in validation["rankings"]} == {24}
    horizon = get_model_performance(artifacts, split="validation", horizon_hours=1)
    assert horizon["horizon_hours"] == 1
    with pytest.raises(AnalyticsToolError):
        get_model_performance(artifacts, split="both")


def test_short_demand_and_weather_unavailable_results_are_honest(artifacts: AnalyticsArtifactStore) -> None:
    demand = analyze_demand_patterns(artifacts); weather = get_weather_context(artifacts)
    assert demand["coverage_hours"] == 24
    assert "not a long-term" in demand["limitation"]
    assert weather["numeric_weather_available"] is False
    assert weather["status"] == "metadata_only"


def test_invalid_model_and_discovery_are_safe(artifacts: AnalyticsArtifactStore) -> None:
    with pytest.raises(AnalyticsToolError, match="Unknown replay model"):
        get_forecast_summary(artifacts, model_id="not-a-model")
    schemas = tool_schemas()
    assert set(schemas) == {"get_forecast_summary", "compare_model_forecasts", "analyze_forecast_errors", "get_model_performance", "analyze_demand_patterns", "get_weather_context"}
    assert all(schema["network_or_model_execution"] is False for schema in schemas.values())
