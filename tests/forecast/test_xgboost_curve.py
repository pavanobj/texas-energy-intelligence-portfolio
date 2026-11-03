from __future__ import annotations

import pandas as pd

from texas_energy.forecast.xgboost_curve import (
    build_issued_curve_demand_examples,
    weather_aware_feature_columns,
)


def _gold(hours: int = 240, *, drop_hour: int | None = None) -> pd.DataFrame:
    periods = pd.date_range("2025-01-01", periods=hours, freq="h", tz="UTC")
    if drop_hour is not None:
        periods = periods.delete(drop_hour)
    values = list(range(40_000, 40_000 + len(periods)))
    rows: dict[str, object] = {"period_utc": periods, "observed_average_demand_mw": values}
    for column in (
        "demand_lag_1h_mwh", "demand_lag_24h_mwh", "demand_lag_168h_mwh",
        "demand_trailing_mean_24h_mwh", "demand_trailing_mean_168h_mwh",
    ):
        rows[column] = [None if index < 168 else float(values[index] - 1) for index in range(len(periods))]
    return pd.DataFrame(rows)


def test_issued_curve_uses_context_end_not_issuance_demand() -> None:
    prepared = build_issued_curve_demand_examples(_gold())
    row = prepared.examples.iloc[0]
    assert row["target_timestamp_utc"] - row["forecast_origin_utc"] == pd.Timedelta(hours=1)
    assert row["forecast_origin_utc"] - row["context_end_utc"] == pd.Timedelta(hours=24)
    assert row["context_demand_lag_1h_mwh"] != row["target_average_demand_mw"]
    assert prepared.examples["horizon_hours"].isin(range(1, 25)).all()


def test_missing_hour_removes_curve_rows_that_need_missing_context_or_target() -> None:
    prepared = build_issued_curve_demand_examples(_gold(drop_hour=190))
    missing = pd.Timestamp("2025-01-08T22:00:00Z")
    assert not prepared.examples["target_timestamp_utc"].eq(missing).any()
    assert not prepared.examples["context_end_utc"].eq(missing).any()


def test_weather_feature_contract_contains_only_pit_weather_and_context_features() -> None:
    columns = weather_aware_feature_columns(("dallas", "austin"))
    assert "context_demand_lag_1h_mwh" in columns
    assert "weather_dallas_temperature_2m_c" in columns
    assert "weather_austin_wind_speed_10m_kmh" in columns
    assert "observed_average_demand_mw" not in columns
