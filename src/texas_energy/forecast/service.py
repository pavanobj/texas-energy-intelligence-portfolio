"""Framework-free local forecast service used by FastAPI and smoke tests."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from .specification import MODEL_SPECS


class ForecastingModel(Protocol):
    def predict(self, target_utc: datetime) -> float: ...


@dataclass(frozen=True)
class ForecastResponse:
    model_name: str
    origin_utc: datetime
    target_utc: datetime
    horizon_hours: int
    prediction_mw: float


class ForecastService:
    """Validate a request against preserved model horizon contracts before serving."""

    def __init__(self, models: Mapping[str, ForecastingModel]) -> None:
        self._models = dict(models)

    def forecast(self, *, model_name: str, origin_utc: datetime, horizon_hours: int) -> ForecastResponse:
        specification = MODEL_SPECS.get(model_name)
        if specification is None:
            raise ValueError("Unknown model name.")
        if horizon_hours != specification.horizon_hours:
            raise ValueError("Requested horizon does not match this model's evaluated contract.")
        if origin_utc.tzinfo is None or origin_utc.utcoffset() != timedelta(0):
            raise ValueError("Forecast origins must be UTC-aware.")
        model = self._models.get(model_name)
        if model is None:
            raise LookupError("Model is not loaded.")
        target = origin_utc + timedelta(hours=horizon_hours)
        return ForecastResponse(model_name, origin_utc, target, horizon_hours, float(model.predict(target)))
