from __future__ import annotations

from datetime import datetime, timezone

import pytest

from texas_energy.forecast.evaluation import evaluate_predictions
from texas_energy.forecast.specification import MODEL_SPECS
from texas_energy.ml.temporal import DatasetSplit, ForecastPrediction


def _prediction(prediction: float, actual: float) -> ForecastPrediction:
    timestamp = datetime(2020, 1, 1, tzinfo=timezone.utc)
    return ForecastPrediction("naive_persistence", DatasetSplit.TRAIN, timestamp, timestamp, 1, prediction, actual)


def test_evaluates_naive_baseline_and_preserves_model_horizons() -> None:
    metrics = evaluate_predictions((_prediction(10, 8), _prediction(4, 5)))
    assert metrics.count == 2
    assert metrics.mae_mw == 1.5
    assert MODEL_SPECS["xgboost_v2"].horizon_hours == 24
    assert MODEL_SPECS["bilstm_v2"].context_hours == 168
    assert MODEL_SPECS["chronos_2"].context_hours == 2048


def test_rejects_mixed_evaluation_cohorts() -> None:
    mixed = _prediction(1, 1)
    other = ForecastPrediction("xgboost_v2", DatasetSplit.TRAIN, mixed.origin_utc, mixed.target_utc, 24, 1, 1)
    with pytest.raises(ValueError, match="share one model"):
        evaluate_predictions((mixed, other))
