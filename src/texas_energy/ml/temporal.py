"""Leakage-safe, model-agnostic UTC temporal preparation utilities."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from enum import Enum


class TemporalContractError(ValueError):
    """Input timestamps cannot safely support a causal forecast example."""


class DatasetSplit(str, Enum):
    TRAIN = "train"
    VALIDATION = "validation"
    TEST = "test"


@dataclass(frozen=True)
class ChronologicalSplit:
    """Inclusive UTC target-time boundaries for train, validation, and test."""

    train_end_utc: datetime
    validation_end_utc: datetime
    test_end_utc: datetime

    def __post_init__(self) -> None:
        boundaries = (self.train_end_utc, self.validation_end_utc, self.test_end_utc)
        if any(not _is_utc_hour(value) for value in boundaries):
            raise TemporalContractError("Split boundaries must be UTC-aware hourly timestamps.")
        if not self.train_end_utc < self.validation_end_utc < self.test_end_utc:
            raise TemporalContractError("Split boundaries must be strictly increasing.")

    def classify_target(self, target_utc: datetime) -> DatasetSplit:
        _require_utc_hour(target_utc, "target_utc")
        if target_utc <= self.train_end_utc:
            return DatasetSplit.TRAIN
        if target_utc <= self.validation_end_utc:
            return DatasetSplit.VALIDATION
        if target_utc <= self.test_end_utc:
            return DatasetSplit.TEST
        raise TemporalContractError("Target timestamp falls outside configured chronological splits.")


@dataclass(frozen=True)
class ForecastOrigin:
    """One causal context/target pair; target is always strictly after origin."""

    origin_utc: datetime
    target_utc: datetime
    horizon_hours: int
    context_start_utc: datetime

    def assert_feature_available(self, available_at_utc: datetime) -> None:
        _require_utc_hour(available_at_utc, "available_at_utc")
        if available_at_utc > self.origin_utc:
            raise TemporalContractError("Feature availability is later than the forecast origin.")


@dataclass(frozen=True)
class ForecastPrediction:
    """Comparable prediction/evaluation record for every model and baseline."""

    model_name: str
    split: DatasetSplit
    origin_utc: datetime
    target_utc: datetime
    horizon_hours: int
    prediction: float
    actual: float


def build_forecast_origins(
    timestamps: Sequence[datetime], *, horizon_hours: int, context_hours: int
) -> tuple[ForecastOrigin, ...]:
    """Keep only origins with complete contiguous hourly causal context.

    This retains next-hour (Bi-LSTM/Chronos) and 24-hour-ahead (XGBoost)
    target definitions separately. A missing hour invalidates each affected
    context or target pair rather than allowing a sequence to bridge a gap.
    """

    if isinstance(horizon_hours, bool) or horizon_hours < 1:
        raise TemporalContractError("horizon_hours must be a positive integer.")
    if isinstance(context_hours, bool) or context_hours < 1:
        raise TemporalContractError("context_hours must be a positive integer.")
    normalized = tuple(sorted(timestamps))
    if len(set(normalized)) != len(normalized):
        raise TemporalContractError("Timestamps must be unique.")
    for value in normalized:
        _require_utc_hour(value, "timestamps")
    available = set(normalized)
    origins: list[ForecastOrigin] = []
    for origin in normalized:
        context_start = origin - timedelta(hours=context_hours - 1)
        target = origin + timedelta(hours=horizon_hours)
        if target not in available:
            continue
        if all(context_start + timedelta(hours=index) in available for index in range(context_hours)):
            origins.append(ForecastOrigin(origin, target, horizon_hours, context_start))
    return tuple(origins)


def naive_persistence_predictions(
    values_by_timestamp: Mapping[datetime, float], *, origins: Sequence[ForecastOrigin], split: ChronologicalSplit
) -> tuple[ForecastPrediction, ...]:
    """Prepare fair persistence-baseline records from origin-time demand."""

    records: list[ForecastPrediction] = []
    for origin in origins:
        try:
            prediction, actual = values_by_timestamp[origin.origin_utc], values_by_timestamp[origin.target_utc]
        except KeyError as error:
            raise TemporalContractError("A forecast origin lacks an origin or target value.") from error
        records.append(
            ForecastPrediction("naive_persistence", split.classify_target(origin.target_utc), origin.origin_utc,
                               origin.target_utc, origin.horizon_hours, float(prediction), float(actual))
        )
    return tuple(records)


def _is_utc_hour(value: datetime) -> bool:
    return value.tzinfo is not None and value.utcoffset() == timedelta(0) and not (value.minute or value.second or value.microsecond)


def _require_utc_hour(value: datetime, label: str) -> None:
    if not _is_utc_hour(value):
        raise TemporalContractError(f"{label} must be a UTC-aware hourly timestamp.")
