"""Mock-only coverage tests using valid Bronze schema 1.1 envelopes."""

from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from texas_energy.data.coverage import audit_historical_bronze
from texas_energy.data.silver import BronzeEnvelopeInput
from texas_energy.ingestion.bronze import BronzeSourceTimeRange, bronze_envelope
from texas_energy.ingestion.raw import RawCapture, canonical_json_bytes


_START = datetime(2024, 1, 1, tzinfo=timezone.utc)
_CITIES = {
    "austin": (30.2672, -97.7431),
    "dallas": (32.7767, -96.7970),
    "el_paso": (31.7619, -106.4850),
    "houston": (29.7604, -95.3698),
    "san_antonio": (29.4241, -98.4936),
}


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


def _eia_envelope(*, value: str = "45000", retrieved_at: datetime = _START) -> BronzeEnvelopeInput:
    payload = {
        "response": {
            "data": [
                {
                    "period": (_START + timedelta(hours=index)).strftime("%Y-%m-%dT%H"),
                    "respondent": "ERCO",
                    "type": "D",
                    "value": value,
                    "value-units": "megawatthours",
                }
                for index in range(24)
            ]
        }
    }
    return BronzeEnvelopeInput(
        uri=f"file:///bronze/eia-{value}-{retrieved_at.hour}.json",
        envelope=bronze_envelope(
            _capture(
                source="eia",
                dataset="electricity/rto/region-data",
                payload=payload,
                parameters={"respondent": "ERCO", "type": "D"},
                retrieved_at=retrieved_at,
            ),
            source_time_range=BronzeSourceTimeRange(start=_START, end=_START + timedelta(hours=23)),
            units={"value": "megawatthours"},
        ),
    )


def _weather_envelope(city: str) -> BronzeEnvelopeInput:
    latitude, longitude = _CITIES[city]
    payload = {
        "latitude": latitude,
        "longitude": longitude,
        "hourly": {
            "time": [int((_START + timedelta(hours=index)).timestamp()) for index in range(24)],
            "temperature_2m": [10.0 + index for index in range(24)],
            "relative_humidity_2m": [50 + index % 10 for index in range(24)],
            "wind_speed_10m": [12.0 + index for index in range(24)],
        },
    }
    return BronzeEnvelopeInput(
        uri=f"file:///bronze/weather-{city}.json",
        envelope=bronze_envelope(
            _capture(
                source="open_meteo",
                dataset="historical-weather",
                payload=payload,
                parameters={"latitude": str(latitude), "longitude": str(longitude)},
                retrieved_at=_START + timedelta(days=1),
            ),
            source_time_range=BronzeSourceTimeRange(start=_START, end=_START + timedelta(hours=23)),
            units={"temperature_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "km/h"},
        ),
    )


def _complete_inputs() -> tuple[BronzeEnvelopeInput, ...]:
    return (_eia_envelope(), *(_weather_envelope(city) for city in _CITIES))


def test_coverage_audit_accepts_a_complete_day_of_validated_bronze() -> None:
    audit = audit_historical_bronze(_complete_inputs(), start_date=date(2024, 1, 1), end_date=date(2024, 1, 1))

    assert audit.expected_demand_hours == 24
    assert audit.expected_weather_city_hours == 120
    assert audit.observed_demand_records == 24
    assert audit.observed_historical_weather_records == 120
    assert audit.is_complete_without_revisions


def test_coverage_audit_reports_missing_city_hours_without_fabricating_them() -> None:
    inputs = tuple(item for item in _complete_inputs() if "weather-dallas" not in item.uri)
    audit = audit_historical_bronze(inputs, start_date=date(2024, 1, 1), end_date=date(2024, 1, 1))

    assert len(audit.missing_weather_city_hours) == 24
    assert {key.city for key in audit.missing_weather_city_hours} == {"dallas"}
    assert not audit.is_coverage_complete


def test_coverage_audit_surfaces_duplicate_captures_and_source_revisions() -> None:
    original = _eia_envelope()
    revision = _eia_envelope(value="46000", retrieved_at=_START + timedelta(days=2))
    audit = audit_historical_bronze(
        (*_complete_inputs(), original, revision),
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
    )

    assert len(audit.duplicate_demand_hours) == 24
    assert len(audit.source_revisions) == 24
    assert {revision.provenance_versions for revision in audit.source_revisions} == {2}
    assert audit.is_coverage_complete is False
    assert audit.is_complete_without_revisions is False
