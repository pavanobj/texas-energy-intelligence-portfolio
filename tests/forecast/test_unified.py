from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.catalog import SeasonalNaive24h, resolve_default
from texas_energy.forecast.unified import (
    UnifiedForecastError,
    baseline_rows,
    metrics,
    strict_common_origins,
    validate_predictions,
)


def _rows(model: str, *, values: tuple[float, float] = (100.0, 120.0)) -> pd.DataFrame:
    origins = pd.date_range("2024-01-01", periods=2, freq="h", tz="UTC")
    return pd.DataFrame({
        "model_id": model, "candidate_id": "run", "origin_timestamp_utc": origins,
        "target_timestamp_utc": origins + pd.Timedelta(hours=24), "horizon_hours": 24,
        "actual_demand_mw": values, "predicted_demand_mw": (105.0, 110.0),
        "baseline_predicted_demand_mw": (100.0, 100.0), "evaluation_split": "validation",
        "generation_type": "historical_backtest",
    })


def test_metrics_hand_calculation_and_zero_safe_mape() -> None:
    value = metrics([0.0, 100.0], [10.0, 80.0])
    assert value.mae_mw == 15.0
    assert value.rmse_mw == pytest.approx((250) ** 0.5)
    assert value.mape_pct == 20.0
    assert value.smape_pct == pytest.approx((200 + (40 / 180 * 100)) / 2)


def test_invalid_horizon_and_duplicate_predictions_fail() -> None:
    invalid = _rows("a")
    invalid.loc[0, "horizon_hours"] = 1
    with pytest.raises(UnifiedForecastError, match="horizon"):
        validate_predictions(invalid)
    duplicate = pd.concat([_rows("a"), _rows("a")], ignore_index=True)
    with pytest.raises(UnifiedForecastError, match="Duplicate"):
        validate_predictions(duplicate)


def test_common_origins_and_baseline_require_exact_agreement() -> None:
    a, b = _rows("a"), _rows("b")
    b = b.iloc[:1]
    common = strict_common_origins(pd.concat([a, b]), model_ids=("a", "b"))
    assert len(common) == 2
    baseline = baseline_rows(common)
    assert len(baseline) == 1
    assert baseline.iloc[0].model_id == "seasonal_naive_24h"


def test_baseline_serving_contract_and_default_catalog_resolution() -> None:
    result = SeasonalNaive24h().forecast(
        origin_timestamp_utc=pd.Timestamp("2025-01-01T00:00:00Z").to_pydatetime(), demand_at_origin_mw=44_000
    )
    assert result["target_timestamp_utc"].startswith("2025-01-02T00:00:00")
    catalog = {"schema_version": "1.0", "models": [{"model_id": "baseline", "default_selection_status": "selected_default"}]}
    assert resolve_default(catalog)["model_id"] == "baseline"
