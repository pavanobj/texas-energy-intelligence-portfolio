from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.weather_features import build_point_in_time_weather_features
from texas_energy.forecast.weather_forecasts import (
    WeatherForecastContractError,
    parse_single_run_response,
    records_frame,
)


def _payload() -> dict[str, object]:
    times = [1735689600 + hour * 3600 for hour in range(48)]
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "unixtime", "temperature_2m": "°C", "relative_humidity_2m": "%",
            "apparent_temperature": "°C", "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": times,
            "temperature_2m": [float(hour) for hour in range(48)],
            "relative_humidity_2m": [50.0] * 48,
            "apparent_temperature": [float(hour) - 1 for hour in range(48)],
            "wind_speed_10m": [10.0] * 48,
        },
    }


def _weather() -> pd.DataFrame:
    rows = []
    for city in ("dallas", "austin"):
        rows.extend(parse_single_run_response(
            _payload(), city=city, model_run_utc="2025-01-01T00:00:00Z",
            retrieval_time_utc="2026-10-19T00:00:00Z",
        ))
    return records_frame(rows)


def test_point_in_time_weather_features_selects_target_weather_available_at_origin() -> None:
    request = pd.DataFrame({
        "forecast_origin_utc": ["2025-01-01T06:00:00Z"],
        "target_timestamp_utc": ["2025-01-01T07:00:00Z"],
        "horizon_hours": [1],
    })
    joined = build_point_in_time_weather_features(request, _weather(), expected_cities=("dallas", "austin"))
    assert len(joined.features) == 1
    assert joined.features.loc[0, "weather_dallas_temperature_2m_c"] == 7.0
    assert joined.features.loc[0, "weather_austin_temperature_2m_c"] == 7.0
    assert len(joined.selected_weather) == 2
    assert joined.selected_weather["weather_available_at_utc"].eq(pd.Timestamp("2025-01-01T06:00:00Z")).all()


def test_feature_rows_are_deterministically_target_sorted() -> None:
    request = pd.DataFrame({
        "forecast_origin_utc": ["2025-01-01T06:00:00Z", "2025-01-01T06:00:00Z"],
        "target_timestamp_utc": ["2025-01-01T08:00:00Z", "2025-01-01T07:00:00Z"],
        "horizon_hours": [2, 1],
    })
    joined = build_point_in_time_weather_features(request, _weather(), expected_cities=("dallas", "austin"))
    assert joined.features["horizon_hours"].tolist() == [1, 2]


def test_point_in_time_weather_features_rejects_weather_not_yet_available() -> None:
    request = pd.DataFrame({
        "forecast_origin_utc": ["2025-01-01T05:00:00Z"],
        "target_timestamp_utc": ["2025-01-01T06:00:00Z"],
    })
    with pytest.raises(WeatherForecastContractError, match="available at issuance"):
        build_point_in_time_weather_features(request, _weather(), expected_cities=("dallas", "austin"))
