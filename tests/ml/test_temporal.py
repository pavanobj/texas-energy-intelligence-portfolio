from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from texas_energy.ml.temporal import ChronologicalSplit, DatasetSplit, TemporalContractError, build_forecast_origins, naive_persistence_predictions


def _hours(count: int) -> tuple[datetime, ...]:
    start = datetime(2024, 1, 1, tzinfo=timezone.utc)
    return tuple(start + timedelta(hours=offset) for offset in range(count))


def test_origins_keep_one_hour_and_24_hour_targets_distinct_and_causal() -> None:
    timestamps = _hours(30)
    next_hour = build_forecast_origins(timestamps, horizon_hours=1, context_hours=3)
    day_ahead = build_forecast_origins(timestamps, horizon_hours=24, context_hours=3)
    assert (next_hour[0].origin_utc, next_hour[0].target_utc) == (timestamps[2], timestamps[3])
    assert (day_ahead[0].origin_utc, day_ahead[0].target_utc) == (timestamps[2], timestamps[26])
    with pytest.raises(TemporalContractError, match="later than the forecast origin"):
        day_ahead[0].assert_feature_available(day_ahead[0].target_utc)


def test_missing_hour_prevents_context_or_target_from_crossing_gap() -> None:
    timestamps = list(_hours(12))
    missing = timestamps.pop(5)
    origins = build_forecast_origins(timestamps, horizon_hours=1, context_hours=4)
    assert all(not (origin.context_start_utc <= missing <= origin.origin_utc) for origin in origins)
    assert all(origin.target_utc - origin.origin_utc == timedelta(hours=1) for origin in origins)


def test_naive_baseline_uses_origin_value_and_target_based_split() -> None:
    timestamps = _hours(6)
    origins = build_forecast_origins(timestamps, horizon_hours=1, context_hours=2)
    values = {timestamp: 100.0 + index for index, timestamp in enumerate(timestamps)}
    split = ChronologicalSplit(timestamps[2], timestamps[4], timestamps[5])
    records = naive_persistence_predictions(values, origins=origins, split=split)
    assert (records[0].prediction, records[0].actual, records[0].split) == (101.0, 102.0, DatasetSplit.TRAIN)
    assert records[-1].split is DatasetSplit.TEST
