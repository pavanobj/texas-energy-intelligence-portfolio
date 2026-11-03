from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.xgboost_v2 import (
    CAUSAL_DEMAND_FEATURES,
    FEATURE_COLUMNS,
    XGBoostV2ContractError,
    build_supervised_examples,
    split_examples,
)


def _gold(hours: int = 220, *, missing_hour: int | None = None) -> pd.DataFrame:
    periods = pd.date_range("2020-01-01", periods=hours, freq="h", tz="UTC")
    if missing_hour is not None:
        periods = periods.delete(missing_hour)
    rows = {"period_utc": periods, "observed_average_demand_mw": range(40_000, 40_000 + len(periods))}
    for column in CAUSAL_DEMAND_FEATURES:
        rows[column] = [None if index < 168 or (missing_hour is not None and index < missing_hour + 168) else 40_000.0 for index in range(len(periods))]
    return pd.DataFrame(rows)


def test_examples_are_24_hour_ahead_and_exclude_target_time_weather() -> None:
    prepared = build_supervised_examples(_gold())
    assert not prepared.examples.empty
    assert (prepared.examples["target_utc"] - prepared.examples["origin_utc"]).eq(pd.Timedelta(hours=24)).all()
    assert "observed_average_demand_mw" not in FEATURE_COLUMNS
    assert all("temperature" not in name and "humidity" not in name for name in FEATURE_COLUMNS)


def test_gap_boundary_null_context_is_not_used() -> None:
    prepared = build_supervised_examples(_gold(missing_hour=180))
    assert prepared.excluded_incomplete_causal_context > 0
    assert not ((prepared.examples["origin_utc"] >= pd.Timestamp("2020-01-08 12:00", tz="UTC")) &
                (prepared.examples["origin_utc"] < pd.Timestamp("2020-01-15 12:00", tz="UTC"))).any()


def test_missing_required_gold_feature_is_rejected() -> None:
    with pytest.raises(XGBoostV2ContractError, match="lacks required"):
        build_supervised_examples(_gold().drop(columns=[CAUSAL_DEMAND_FEATURES[0]]))


def test_split_uses_target_timestamp() -> None:
    frame = pd.DataFrame({
        "target_utc": pd.to_datetime(["2023-12-31 23:00Z", "2024-01-01 00:00Z", "2025-01-01 00:00Z"]),
        "origin_utc": pd.to_datetime(["2023-12-30 23:00Z", "2023-12-31 00:00Z", "2024-12-31 00:00Z"]),
    })
    parts = split_examples(frame)
    assert [len(parts[name]) for name in ("train", "validation", "test")] == [1, 1, 1]
