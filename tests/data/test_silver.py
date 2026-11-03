"""Unit tests for Bronze-envelope parsing and deterministic Silver normalization."""

from __future__ import annotations

from datetime import datetime, timezone
from hashlib import sha256
import json

import pytest

from texas_energy.data.silver import (
    BronzeEnvelopeInput,
    EIA_HOURLY_ENERGY_UNIT,
    SilverValidationError,
    load_local_bronze_envelopes,
    transform_bronze_envelopes,
)
from texas_energy.ingestion.bronze import BronzeSourceTimeRange, bronze_envelope
from texas_energy.ingestion.raw import RawCapture, canonical_json_bytes


def _capture(*, source: str, dataset: str, payload: dict[str, object], parameters: dict[str, str], retrieved_at: datetime) -> RawCapture:
    return RawCapture(
        source=source,
        dataset=dataset,
        endpoint="https://example.invalid/data",
        request_parameters=parameters,
        raw_payload=canonical_json_bytes(payload),
        retrieved_at=retrieved_at,
        status_code=200,
        response_headers={"Content-Type": "application/json"},
    )


def _eia_envelope(
    *,
    value: str = "45000",
    retrieved_at: datetime | None = None,
    periods: list[str] | None = None,
    respondent: str = "ERCO",
    demand_type: str = "D",
    value_unit: str = EIA_HOURLY_ENERGY_UNIT,
) -> dict[str, object]:
    periods = periods or ["2024-01-01T00", "2024-01-01T01"]
    payload = {
        "response": {
            "data": [
                {
                    "period": period,
                    "respondent": respondent,
                    "type": demand_type,
                    "value": value,
                    "value-units": value_unit,
                }
                for period in periods
            ]
        }
    }
    capture = _capture(
        source="eia",
        dataset="electricity/rto/region-data",
        payload=payload,
        parameters={"respondent": "ERCO", "type": "D"},
        retrieved_at=retrieved_at or datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    return bronze_envelope(
        capture,
        source_time_range=BronzeSourceTimeRange(
            start=datetime.fromisoformat(periods[0] + ":00+00:00"),
            end=datetime.fromisoformat(periods[-1] + ":00+00:00"),
        ),
        units={"value": value_unit},
    )


_CITY_COORDINATES = {
    "austin": ("30.2672", "-97.7431", 30.263618, -97.69321),
    "dallas": ("32.7767", "-96.7970", 32.794376, -96.8215),
    "el_paso": ("31.7619", "-106.485", 31.739893, -106.42258),
    "houston": ("29.7604", "-95.3698", 29.771528, -95.33099),
    "san_antonio": ("29.4241", "-98.4936", 29.420034, -98.52194),
}


def _weather_envelope(
    dataset: str = "historical-weather",
    *,
    city: str = "dallas",
    retrieved_at: datetime | None = None,
    provider_latitude: float | None = None,
    provider_longitude: float | None = None,
    model: str | None = None,
) -> dict[str, object]:
    requested_latitude, requested_longitude, expected_provider_latitude, expected_provider_longitude = _CITY_COORDINATES[city]
    payload = {
        "latitude": provider_latitude if provider_latitude is not None else expected_provider_latitude,
        "longitude": provider_longitude if provider_longitude is not None else expected_provider_longitude,
        "hourly": {
            "time": [1704067200, 1704070800],
            "temperature_2m": [10.0, 11.0],
            "relative_humidity_2m": [50, 51],
            "wind_speed_10m": [12.0, 13.0],
        }
    }
    parameters = {"latitude": requested_latitude, "longitude": requested_longitude}
    if model is not None:
        parameters["models"] = model
    capture = _capture(
        source="open_meteo",
        dataset=dataset,
        payload=payload,
        parameters=parameters,
        retrieved_at=retrieved_at or datetime(2024, 1, 2, tzinfo=timezone.utc),
    )
    return bronze_envelope(
        capture,
        source_time_range=BronzeSourceTimeRange(
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
        ),
        units={"temperature_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "km/h"},
    )


def test_transforms_actual_bronze_envelope_shapes_into_typed_demand_and_separate_weather_sets() -> None:
    result = transform_bronze_envelopes(
        (
            BronzeEnvelopeInput("file:///bronze/eia.json", _eia_envelope()),
            BronzeEnvelopeInput("file:///bronze/weather-history.json", _weather_envelope()),
            BronzeEnvelopeInput("file:///bronze/weather-forecast.json", _weather_envelope("forecast")),
        )
    )

    assert len(result.demand) == 2
    assert result.demand[0].period_utc == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert str(result.demand[0].demand_value) == "45000"
    assert result.demand[0].demand_unit == EIA_HOURLY_ENERGY_UNIT
    assert result.demand[0].respondent == "ERCO"
    assert result.demand[0].demand_type == "D"
    assert result.demand[0].average_demand_mw == result.demand[0].demand_value
    assert len(result.historical_weather) == 2
    assert result.historical_weather[0].city == "dallas"
    assert result.historical_weather[0].requested_longitude == -96.797
    assert result.historical_weather[0].provider_longitude == -96.8215
    assert result.historical_weather[0].provider_coordinate_distance_km < 10
    assert result.historical_weather[0].temperature_2m == 10.0
    assert result.historical_weather[0].relative_humidity_unit == "%"
    assert len(result.forecast_weather) == 2
    assert result.forecast_weather[0].dataset == "forecast"
    assert result.as_rows()["demand"][0]["quality_flags"] == []


def test_issued_ifs_weather_retains_model_run_and_conservative_availability_lineage() -> None:
    envelope = _weather_envelope("issued-forecast-weather")
    envelope["capture"]["request_parameters"].update({"models": "ecmwf_ifs", "run": "2024-01-01T00:00"})  # type: ignore[index]
    envelope["source_payload"]["hourly"]["apparent_temperature"] = [9.0, 10.0]  # type: ignore[index]
    envelope["units"]["apparent_temperature"] = "°C"  # type: ignore[index]
    # The Bronze sanitized-payload digest must remain the immutable source
    # identity after adding the model-specific variable to this unit fixture.
    envelope["sanitized_payload_sha256"] = sha256(canonical_json_bytes(envelope["source_payload"])).hexdigest()

    result = transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/ifs.json", envelope),))

    assert len(result.issued_forecast_weather) == 2
    record = result.issued_forecast_weather[0]
    assert record.weather_model == "ecmwf_ifs"
    assert record.weather_model_run_utc == datetime(2024, 1, 1, tzinfo=timezone.utc)
    assert record.weather_available_at_utc == datetime(2024, 1, 1, 6, tzinfo=timezone.utc)
    assert record.apparent_temperature == 9.0


@pytest.mark.parametrize("city", tuple(_CITY_COORDINATES))
def test_resolves_each_configured_city_from_request_coordinates_and_accepts_its_provider_grid_offset(city: str) -> None:
    result = transform_bronze_envelopes((BronzeEnvelopeInput(f"file:///bronze/{city}.json", _weather_envelope(city=city)),))

    assert {record.city for record in result.historical_weather} == {city}
    assert all(record.provider_coordinate_distance_km <= 10 for record in result.historical_weather)


def test_weather_city_validation_rejects_unknown_or_invalid_coordinates() -> None:
    unknown = _weather_envelope()
    unknown["capture"]["request_parameters"]["latitude"] = "0"  # type: ignore[index]
    with pytest.raises(SilverValidationError, match="not one configured"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/unknown.json", unknown),))

    invalid_provider = _weather_envelope(provider_latitude=91.0)
    with pytest.raises(SilverValidationError, match="outside its valid geographic range"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/invalid.json", invalid_provider),))

    distant_provider = _weather_envelope(provider_latitude=31.0, provider_longitude=-97.0)
    with pytest.raises(SilverValidationError, match="too distant"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/distant.json", distant_provider),))


def test_era5_historical_grid_bound_is_narrowly_model_specific() -> None:
    # Houston's ERA5 0.25-degree grid cell is 11.622 km from the requested
    # city point: valid only when the immutable request explicitly pins ERA5.
    era5 = _weather_envelope(
        city="houston", provider_latitude=29.75, provider_longitude=-95.25, model="era5"
    )
    result = transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/era5.json", era5),))
    assert result.historical_weather[0].provider_coordinate_distance_km == pytest.approx(11.6225, abs=0.001)

    unpinned = _weather_envelope(city="houston", provider_latitude=29.75, provider_longitude=-95.25)
    with pytest.raises(SilverValidationError, match="too distant"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/unpinned.json", unpinned),))

    wrong_city = _weather_envelope(city="houston", provider_latitude=32.75, provider_longitude=-96.75, model="era5")
    with pytest.raises(SilverValidationError, match="too distant"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/wrong-city.json", wrong_city),))


def test_weather_rerun_deduplicates_by_city_dataset_and_timestamp_with_newer_provenance() -> None:
    original = _weather_envelope(retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    revised = _weather_envelope(retrieved_at=datetime(2024, 1, 3, tzinfo=timezone.utc))
    result = transform_bronze_envelopes(
        (
            BronzeEnvelopeInput("file:///bronze/original.json", original),
            BronzeEnvelopeInput("file:///bronze/revised.json", revised),
        )
    )

    assert len(result.historical_weather) == 2
    assert {record.bronze_uri for record in result.historical_weather} == {"file:///bronze/revised.json"}


def test_later_ingested_duplicate_business_keys_are_selected_deterministically_for_reruns() -> None:
    earlier = _eia_envelope(value="45000", retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    later = _eia_envelope(value="46000", retrieved_at=datetime(2024, 1, 3, tzinfo=timezone.utc))
    result = transform_bronze_envelopes(
        (BronzeEnvelopeInput("file:///bronze/earlier.json", earlier), BronzeEnvelopeInput("file:///bronze/later.json", later))
    )

    assert len(result.demand) == 2
    assert {str(record.demand_value) for record in result.demand} == {"46000"}


def test_rejects_missing_hours_and_payload_hash_tampering() -> None:
    missing_hour = _eia_envelope(periods=["2024-01-01T00", "2024-01-01T02"])
    with pytest.raises(SilverValidationError, match="missing, duplicate, or non-hourly"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/bad-hours.json", missing_hour),))

    tampered = _eia_envelope()
    tampered["source_payload"] = {"response": {"data": []}}
    with pytest.raises(SilverValidationError, match="hash"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/tampered.json", tampered),))


@pytest.mark.parametrize(
    ("respondent", "demand_type"),
    (("OTHER", "D"), ("ERCO", "OTHER")),
)
def test_rejects_noncanonical_eia_respondent_or_demand_type(respondent: str, demand_type: str) -> None:
    envelope = _eia_envelope(respondent=respondent, demand_type=demand_type)

    with pytest.raises(SilverValidationError, match="respondent ERCO and demand type D"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/invalid-eia.json", envelope),))


def test_rejects_noncanonical_eia_value_unit_without_relabeling_it() -> None:
    envelope = _eia_envelope(value_unit="megawatts")

    with pytest.raises(SilverValidationError, match="authoritative megawatthours"):
        transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/invalid-unit.json", envelope),))


def test_local_bronze_loader_reads_json_envelopes_without_s3(tmp_path) -> None:
    path = tmp_path / "sample.json"
    path.write_text(json.dumps(_eia_envelope()), encoding="utf-8")

    loaded = load_local_bronze_envelopes(tmp_path)

    assert len(loaded) == 1
    assert loaded[0].uri == path.resolve().as_uri()
