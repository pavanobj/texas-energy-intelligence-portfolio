"""Local, model-neutral forecasting contracts and optional integrations."""

from .evaluation import EvaluationMetrics, evaluate_predictions
from .service import ForecastResponse, ForecastService
from .specification import MODEL_SPECS, ForecastModelSpec

__all__ = [
    "EvaluationMetrics", "ForecastModelSpec", "ForecastResponse", "ForecastService", "MODEL_SPECS", "evaluate_predictions"
]
