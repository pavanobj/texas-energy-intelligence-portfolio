"""Small CPU-only local training contract for pre-model data validation.

This is intentionally a smoke-training implementation, not a replacement for
the notebook XGBoost, Bi-LSTM, or Chronos experiments.  It trains an
hour-of-week mean model using only training timestamps, which proves the
dataset/origin/evaluation/artifact flow without suggesting one-month metrics
are useful model evidence.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
import json
from pathlib import Path

from texas_energy.ml.temporal import ChronologicalSplit, ForecastOrigin, ForecastPrediction


class TrainingContractError(ValueError):
    """Training input cannot support a point-in-time-safe local experiment."""


@dataclass(frozen=True)
class HourOfWeekMeanModel:
    """Serializable baseline-like trained model indexed by target UTC weekday/hour."""

    hourly_means_mw: Mapping[str, float]
    global_mean_mw: float

    def predict(self, target_utc: datetime) -> float:
        key = _hour_of_week_key(target_utc)
        return float(self.hourly_means_mw.get(key, self.global_mean_mw))

    def save(self, path: Path) -> None:
        payload = {
            "schema_version": 1,
            "model_type": "hour_of_week_mean",
            "global_mean_mw": self.global_mean_mw,
            "hourly_means_mw": dict(sorted(self.hourly_means_mw.items())),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, sort_keys=True, separators=(",", ":")), encoding="utf-8")


def fit_hour_of_week_mean(
    values_by_timestamp: Mapping[datetime, float], *, split: ChronologicalSplit
) -> HourOfWeekMeanModel:
    """Fit solely on values whose timestamp belongs to the chronological train split."""

    groups: dict[str, list[float]] = defaultdict(list)
    all_values: list[float] = []
    for timestamp, value in values_by_timestamp.items():
        if split.classify_target(timestamp).value != "train":
            continue
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            raise TrainingContractError("Training demand values must be numeric.")
        numeric = float(value)
        groups[_hour_of_week_key(timestamp)].append(numeric)
        all_values.append(numeric)
    if not all_values:
        raise TrainingContractError("No chronological training values are available.")
    return HourOfWeekMeanModel(
        hourly_means_mw={key: sum(values) / len(values) for key, values in groups.items()},
        global_mean_mw=sum(all_values) / len(all_values),
    )


def predict_origins(
    *,
    model: HourOfWeekMeanModel,
    model_name: str,
    origins: Sequence[ForecastOrigin],
    values_by_timestamp: Mapping[datetime, float],
    split: ChronologicalSplit,
) -> tuple[ForecastPrediction, ...]:
    """Evaluate model predictions at prevalidated origins without target leakage."""

    records: list[ForecastPrediction] = []
    for origin in origins:
        try:
            actual = float(values_by_timestamp[origin.target_utc])
        except KeyError:
            raise TrainingContractError("Forecast target is unavailable.") from None
        records.append(
            ForecastPrediction(
                model_name=model_name,
                split=split.classify_target(origin.target_utc),
                origin_utc=origin.origin_utc,
                target_utc=origin.target_utc,
                horizon_hours=origin.horizon_hours,
                prediction=model.predict(origin.target_utc),
                actual=actual,
            )
        )
    return tuple(records)


def synthetic_hourly_demand(*, start_utc: datetime, hours: int) -> dict[datetime, float]:
    """Small deterministic fixture appropriate only for smoke-training execution."""

    if hours < 1:
        raise TrainingContractError("Synthetic duration must be positive.")
    return {
        start_utc + timedelta(hours=index): 40_000.0 + (index % 24) * 100.0 + (index // 24 % 7) * 20.0
        for index in range(hours)
    }


def _hour_of_week_key(timestamp: datetime) -> str:
    if timestamp.tzinfo is None or timestamp.utcoffset() != timedelta(0):
        raise TrainingContractError("Timestamps must be UTC-aware.")
    return f"{timestamp.weekday()}-{timestamp.hour}"
