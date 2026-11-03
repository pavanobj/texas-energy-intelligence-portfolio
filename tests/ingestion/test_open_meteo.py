from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from texas_energy.ingestion.errors import ContractError
from texas_energy.ingestion.http import RetryingJsonClient
from texas_energy.ingestion.open_meteo import (
    HISTORICAL_HOURLY_FIELDS,
    HISTORICAL_WEATHER_MODEL,
    OPEN_METEO_FORECAST_ENDPOINT,
    OPEN_METEO_HISTORICAL_ENDPOINT,
    OPEN_METEO_SINGLE_RUNS_ENDPOINT,
    TexasCity,
    WeatherClient,
)

from .conftest import ScriptedTransport, json_response


def _weather_payload(*, times: list[int] | None = None) -> dict[str, object]:
    time_values = times or [1_704_067_200, 1_704_070_800]
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": time_values,
            "temperature_2m": [10.0, 11.0],
            "relative_humidity_2m": [50, 51],
            "wind_speed_10m": [12.0, 13.0],
        },
    }


def test_all_five_notebook_reference_cities_are_available() -> None:
    assert [city.slug for city in TexasCity] == [
        "austin",
        "dallas",
        "el_paso",
        "houston",
        "san_antonio",
    ]


def test_historical_request_uses_explicit_fields_and_utc_unix_timestamps() -> None:
    transport = ScriptedTransport([json_response(_weather_payload())])
    client = WeatherClient(
        RetryingJsonClient(transport),
        retrieved_at=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    result = client.fetch_historical(
        TexasCity.DALLAS,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
    )

    request = transport.requests[0]
    assert request.url == OPEN_METEO_HISTORICAL_ENDPOINT
    assert request.parameters["hourly"] == ",".join(HISTORICAL_HOURLY_FIELDS)
    assert request.parameters["timezone"] == "GMT"
    assert request.parameters["timeformat"] == "unixtime"
    assert request.parameters["models"] == HISTORICAL_WEATHER_MODEL
    assert result.hourly_times_utc[0].tzinfo == timezone.utc
    assert result.capture.manifest()["dataset"] == "historical-weather"
    assert result.capture.manifest()["request_parameters"]["models"] == HISTORICAL_WEATHER_MODEL


def test_forecast_request_has_a_bounded_requested_horizon() -> None:
    transport = ScriptedTransport([json_response(_weather_payload())])
    client = WeatherClient(
        RetryingJsonClient(transport),
        retrieved_at=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    client.fetch_forecast(TexasCity.AUSTIN, forecast_hours=48)

    request = transport.requests[0]
    assert request.url == OPEN_METEO_FORECAST_ENDPOINT
    assert request.parameters["forecast_hours"] == "48"


def test_issued_ifs_request_preserves_an_explicit_run_and_four_model_fields() -> None:
    payload = _weather_payload()
    payload["hourly_units"]["apparent_temperature"] = "°C"  # type: ignore[index]
    payload["hourly"]["apparent_temperature"] = [9.0, 10.0]  # type: ignore[index]
    transport = ScriptedTransport([json_response(payload)])
    client = WeatherClient(
        RetryingJsonClient(transport),
        retrieved_at=lambda: datetime(2024, 1, 1, 6, tzinfo=timezone.utc),
    )

    result = client.fetch_issued_ifs(
        TexasCity.DALLAS,
        model_run_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        forecast_hours=2,
    )

    request = transport.requests[0]
    assert request.url == OPEN_METEO_SINGLE_RUNS_ENDPOINT
    assert request.parameters["models"] == "ecmwf_ifs"
    assert request.parameters["run"] == "2024-01-01T00:00"
    assert request.parameters["forecast_hours"] == "2"
    assert request.parameters["hourly"] == "temperature_2m,relative_humidity_2m,apparent_temperature,wind_speed_10m"
    assert result.capture.manifest()["dataset"] == "issued-forecast-weather"


def test_weather_rejects_misaligned_fields_and_non_utc_response() -> None:
    malformed = _weather_payload()
    malformed["hourly"]["wind_speed_10m"] = [12.0]  # type: ignore[index]
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([json_response(malformed)])))

    with pytest.raises(ContractError, match="wind_speed_10m"):
        client.fetch_forecast(TexasCity.HOUSTON)

    non_utc = _weather_payload()
    non_utc["utc_offset_seconds"] = -21_600
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([json_response(non_utc)])))

    with pytest.raises(ContractError, match="utc_offset_seconds"):
        client.fetch_forecast(TexasCity.HOUSTON)


def test_weather_rejects_non_hourly_timestamps_and_blank_units() -> None:
    non_hourly = _weather_payload(times=[1_704_069_000, 1_704_072_600])
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([json_response(non_hourly)])))

    with pytest.raises(ContractError, match="align exactly"):
        client.fetch_forecast(TexasCity.HOUSTON)

    blank_unit = _weather_payload()
    blank_unit["hourly_units"]["wind_speed_10m"] = ""  # type: ignore[index]
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([json_response(blank_unit)])))

    with pytest.raises(ContractError, match="non-empty unit"):
        client.fetch_forecast(TexasCity.HOUSTON)


def test_weather_rejects_null_hourly_values() -> None:
    null_value = _weather_payload()
    null_value["hourly"]["temperature_2m"] = [10.0, None]  # type: ignore[index]
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([json_response(null_value)])))

    with pytest.raises(ContractError, match="temperature_2m must not contain missing values"):
        client.fetch_forecast(TexasCity.HOUSTON)


def test_historical_weather_retains_missing_value_coverage_for_availability_handling() -> None:
    null_value = _weather_payload()
    null_value["hourly"]["temperature_2m"] = [None, None]  # type: ignore[index]
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([json_response(null_value)])))

    response = client.fetch_historical(
        TexasCity.HOUSTON,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
    )

    assert response.missing_hourly_value_counts == {"temperature_2m": 2}


def test_weather_rejects_timestamps_outside_the_requested_range() -> None:
    historical_client = WeatherClient(
        RetryingJsonClient(ScriptedTransport([json_response(_weather_payload())]))
    )
    with pytest.raises(ContractError, match="historical timestamps"):
        historical_client.fetch_historical(
            TexasCity.DALLAS,
            start_date=date(2024, 1, 2),
            end_date=date(2024, 1, 2),
        )

    forecast_client = WeatherClient(
        RetryingJsonClient(ScriptedTransport([json_response(_weather_payload())])),
        retrieved_at=lambda: datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    with pytest.raises(ContractError, match="forecast timestamps"):
        forecast_client.fetch_forecast(TexasCity.DALLAS, forecast_hours=1)


@pytest.mark.parametrize(
    "fields",
    [(), ("temperature_2m", "temperature_2m"), ("not_a_supported_field",)],
)
def test_weather_rejects_empty_duplicate_or_unsupported_requested_fields(fields: tuple[str, ...]) -> None:
    client = WeatherClient(RetryingJsonClient(ScriptedTransport([])))

    with pytest.raises(ValueError):
        client.fetch_forecast(TexasCity.HOUSTON, fields=fields)
