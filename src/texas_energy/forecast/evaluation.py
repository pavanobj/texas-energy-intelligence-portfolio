"""Framework-free prediction evaluation shared by local model experiments."""

from __future__ import annotations

from dataclasses import dataclass
from math import sqrt

from texas_energy.ml.temporal import ForecastPrediction


@dataclass(frozen=True)
class EvaluationMetrics:
    count: int
    mae_mw: float
    rmse_mw: float


def evaluate_predictions(predictions: tuple[ForecastPrediction, ...]) -> EvaluationMetrics:
    """Calculate comparable metrics only for one model/horizon/split cohort."""

    if not predictions:
        raise ValueError("At least one prediction is required.")
    identities = {(item.model_name, item.horizon_hours, item.split) for item in predictions}
    if len(identities) != 1:
        raise ValueError("Evaluation inputs must share one model, horizon, and chronological split.")
    errors = [item.prediction - item.actual for item in predictions]
    return EvaluationMetrics(
        count=len(errors),
        mae_mw=sum(abs(value) for value in errors) / len(errors),
        rmse_mw=sqrt(sum(value * value for value in errors) / len(errors)),
    )
