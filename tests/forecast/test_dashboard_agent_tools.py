"""Contracts for the fixed published-dashboard AgentCore evidence allowlist."""
from __future__ import annotations

from texas_energy.analytics.dashboard_tools import (
    DashboardDataStore,
    get_historical_backtest_metrics,
    get_latest_prospective_forecast,
    get_rolling_prospective_metrics,
    get_weather_comparison,
)


def _store() -> DashboardDataStore:
    def predictions(offset: int) -> list[dict[str, object]]:
        return [
            {
                "horizon_hours": hour,
                "target_timestamp_utc": f"2026-10-29T{hour:02d}:00:00Z",
                "predicted_demand_mw": 50000 + offset + hour,
                "actual_demand_mw": None,
            }
            for hour in range(1, 25)
        ]
    model_ids = ["weather_aware_xgboost_curve", "weather_aware_bilstm_curve", "amazon/chronos-2"]
    documents = {
        "prospective-forecast.json": {
            "forecast_generation_type": "prospective_live_inference",
            "status": "pending_actuals",
            "issue_id": "20261028T150000Z",
            "forecast_origin_utc": "2026-10-28T15:00:00Z",
            "execution": {"arn": "redacted", "model_execution_completed_at_utc": "2026-10-28T15:14:30Z", "dashboard_published_at_utc": "2026-10-28T15:15:26Z"},
            "actual_coverage": {"matched_hours": 0, "missing_hours": 24},
            "models": [
                {"model_id": model_id, "model_label": model_id, "metrics": None, "predictions": predictions(index * 100)}
                for index, model_id in enumerate(model_ids)
            ],
        },
        "prospective-evaluation-manifest.json": {"latest_issue_id": "20261028T150000Z"},
        "rolling-prospective-evaluation.json": {
            "status": "prospective_evaluation",
            "issues": ["20261026T150000Z", "20261027T150000Z", "20261028T150000Z"],
            "generated_at_utc": "2026-10-28T16:00:00Z",
            "metrics": {"7_day": {"chronos2": {"mae_mw": 1.0}}, "14_day": {"chronos2": {"mae_mw": 1.0}}},
            "weather": {"records": [{"city": "dallas", "issue_id": "20261028T150000Z", "observed_temperature_2m_c": None}]},
        },
        "rolling-historical-replay.json": {
            "issue_start_utc": "2026-10-12T06:00:00Z",
            "issue_end_utc": "2026-10-25T06:00:00Z",
            "issue_count": 14,
            "metrics": {
                "7_day": {"chronos2": {"mae_mw": 2.0, "rmse_mw": 2.5, "bias_mw": -0.5}},
                "14_day": {"chronos2": {"mae_mw": 3.0, "rmse_mw": 3.5, "bias_mw": -1.0}},
            },
            "weather": {"records": [{"city": "Dallas", "issue_id": "20261025T060000Z", "observed_temperature_2m_c": 24.0}]},
        },
    }
    return DashboardDataStore(documents=documents, sha256={key: "a" * 64 for key in documents})


def test_dashboard_tools_preserve_pending_actuals_and_historical_labels() -> None:
    store = _store()
    latest = get_latest_prospective_forecast(store)
    assert latest["issue_id"] == "20261028T150000Z"
    assert latest["forecast_issue_utc"] == "2026-10-28T15:00:00Z"
    assert latest["dashboard_published_at_utc"] == "2026-10-28T15:15:26Z"
    assert latest["model_execution_completed_at_utc"] == "2026-10-28T15:14:30Z"
    assert [len(model["predictions"]) for model in latest["models"]] == [24, 24, 24]
    assert latest["models"][0]["predictions"][0]["actual_demand_mw"] is None
    assert get_rolling_prospective_metrics(store, window_days=14)["historical_not_live"] is False
    historical = get_historical_backtest_metrics(store, window_days=14)
    assert historical["historical_not_live"] is True
    assert historical["metrics"]["chronos2"]["mae_mw"] == 3.0
    assert historical["metrics"]["chronos2"] == {"mae_mw": 3.0, "rmse_mw": 3.5, "bias_mw": -1.0}
    assert {model["model_id"]: model["predicted_peak_demand_mw"] for model in latest["models"]} == {
        "weather_aware_xgboost_curve": 50024.0,
        "weather_aware_bilstm_curve": 50124.0,
        "amazon/chronos-2": 50224.0,
    }
    assert latest["models"][0]["predictions"][-1]["actual_demand_mw"] is None
    assert latest["evidence"][0]["sha256"] == "a" * 64


def test_weather_tool_uses_only_approved_city_and_scope() -> None:
    store = _store()
    prospective = get_weather_comparison(store, city="dallas", scope="latest_prospective")
    historical = get_weather_comparison(store, city="dallas", scope="historical_backtest")
    assert prospective["observation_count"] == 0
    assert prospective["missing_observation_count"] == 1
    assert historical["observation_count"] == 1
    assert historical["historical_not_live"] is True
