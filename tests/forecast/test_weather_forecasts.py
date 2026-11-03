from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.weather_forecasts import (
    WeatherForecastContractError,
    parse_single_run_response,
    parse_previous_day2_response,
    records_frame,
    select_as_of_weather,
)


def _single_run_payload() -> dict[str, object]:
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "unixtime", "temperature_2m": "°C", "relative_humidity_2m": "%",
            "apparent_temperature": "°C", "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": [1735689600, 1735693200],
            "temperature_2m": [10.0, 11.0], "relative_humidity_2m": [50.0, 51.0],
            "apparent_temperature": [9.0, 10.0], "wind_speed_10m": [12.0, 13.0],
        },
    }


def _payload() -> dict[str, object]:
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "time": "iso8601", "temperature_2m_previous_day2": "°C",
            "relative_humidity_2m_previous_day2": "%", "apparent_temperature_previous_day2": "°C",
            "wind_speed_10m_previous_day2": "km/h",
        },
        "hourly": {
            "time": ["2025-01-03T00:00", "2025-01-03T01:00"],
            "temperature_2m_previous_day2": [10.0, 11.0],
            "relative_humidity_2m_previous_day2": [50.0, 51.0],
            "apparent_temperature_previous_day2": [9.0, 10.0],
            "wind_speed_10m_previous_day2": [12.0, 13.0],
        },
    }


def test_previous_day2_records_keep_issue_unknown_and_explicit_availability_basis() -> None:
    records = parse_previous_day2_response(_payload(), city="Dallas", retrieval_time_utc="2025-01-03T12:00:00Z")
    assert len(records) == 2
    assert records[0].weather_forecast_issue_utc is None
    assert records[0].weather_available_at_utc == pd.Timestamp("2025-01-01T12:00:00Z")


def test_as_of_join_rejects_later_weather_revision() -> None:
    weather = records_frame(parse_previous_day2_response(_payload(), city="Dallas", retrieval_time_utc="2025-01-03T12:00:00Z"))
    requests = pd.DataFrame({"city": ["Dallas"], "forecast_origin_utc": ["2025-01-01T11:00:00Z"], "target_timestamp_utc": ["2025-01-03T00:00:00Z"]})
    with pytest.raises(WeatherForecastContractError, match="available at issuance"):
        select_as_of_weather(requests, weather)


def test_as_of_join_accepts_conservatively_available_forecast() -> None:
    weather = records_frame(parse_previous_day2_response(_payload(), city="Dallas", retrieval_time_utc="2025-01-03T12:00:00Z"))
    requests = pd.DataFrame({"city": ["Dallas"], "forecast_origin_utc": ["2025-01-02T00:00:00Z"], "target_timestamp_utc": ["2025-01-03T00:00:00Z"]})
    joined = select_as_of_weather(requests, weather)
    assert joined.loc[0, "temperature_2m_c"] == 10.0


def test_previous_run_payload_rejects_missing_required_weather_variable() -> None:
    payload = _payload()
    del payload["hourly"]["wind_speed_10m_previous_day2"]  # type: ignore[index]
    with pytest.raises(WeatherForecastContractError, match="required"):
        parse_previous_day2_response(payload, city="Dallas", retrieval_time_utc="2025-01-03T12:00:00Z")


def test_single_run_keeps_model_init_distinct_from_provider_availability() -> None:
    records = parse_single_run_response(
        _single_run_payload(), city="Dallas", model_run_utc="2025-01-01T00:00:00Z",
        retrieval_time_utc="2026-10-19T00:00:00Z",
    )
    assert records[0].weather_model_run_utc == pd.Timestamp("2025-01-01T00:00:00Z")
    assert records[0].weather_forecast_issue_utc is None
    assert records[0].weather_available_at_utc == pd.Timestamp("2025-01-01T06:00:00Z")
    assert records[1].forecast_lead_hours == 1


def test_single_run_rejects_valid_time_before_explicit_model_run() -> None:
    with pytest.raises(WeatherForecastContractError, match="precedes"):
        parse_single_run_response(
            _single_run_payload(), city="Dallas", model_run_utc="2025-01-01T01:00:00Z",
            retrieval_time_utc="2026-10-19T00:00:00Z",
        )
