"""Parse versioned Bronze envelopes into typed, validated Silver records.

This module intentionally has no Spark dependency. It validates the exact JSON
envelope written by ``BronzeS3Writer`` so unit tests and local inspection remain
fast while the optional Spark job handles Delta persistence separately.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation
from hashlib import file_digest, sha256
import json
import math
from pathlib import Path
from typing import Literal

from texas_energy.ingestion.raw import canonical_json_bytes
from texas_energy.ingestion.eia import EIA_DEMAND_TYPE, EIA_RESPONDENT


BRONZE_SCHEMA_VERSION = "1.1"
EIA_DATASET = "electricity/rto/region-data"
WEATHER_HISTORICAL_DATASET = "historical-weather"
WEATHER_FORECAST_DATASET = "forecast"
WEATHER_ISSUED_IFS_DATASET = "issued-forecast-weather"
EIA_HOURLY_ENERGY_UNIT = "megawatthours"
EIA_INTERVAL_HOURS = Decimal("1")
WEATHER_FIELDS = ("temperature_2m", "relative_humidity_2m", "wind_speed_10m")
_CONFIGURED_CITIES = (
    ("austin", 30.2672, -97.7431),
    ("dallas", 32.7767, -96.7970),
    ("el_paso", 31.7619, -106.4850),
    ("houston", 29.7604, -95.3698),
    ("san_antonio", 29.4241, -98.4936),
)
# RawCapture preserves request parameters as strings. One millionth of a degree
# (~11 cm latitude) accepts benign numeric serialization without accepting a
# materially different requested location.
_REQUEST_COORDINATE_TOLERANCE_DEGREES = 0.000001
# The default stays deliberately strict for existing higher-resolution captures.
# ERA5 historical requests are explicitly identified in immutable request
# parameters and resolve to a 0.25-degree grid. Its maximum Texas-city offset
# observed in the controlled January 2020 Bronze batch is 11.623 km (Houston);
# 20 km is a conservative grid-cell bound, not a source-agnostic relaxation.
_MAX_PROVIDER_GRID_DISTANCE_KM = 10.0
_MAX_ERA5_PROVIDER_GRID_DISTANCE_KM = 20.0
_OPEN_METEO_ERA5_MODEL = "era5"
_EARTH_RADIUS_KM = 6371.0088


class SilverValidationError(ValueError):
    """A Bronze envelope cannot safely produce a typed Silver record."""


@dataclass(frozen=True)
class BronzeEnvelopeInput:
    """One locally supplied Bronze envelope and a safe source identifier."""

    uri: str
    envelope: Mapping[str, object]


@dataclass(frozen=True)
class DemandSilverRecord:
    """One validated ERCOT hourly demand observation in UTC.

    ``demand_value`` remains the authoritative EIA interval-energy value.  The
    model-facing average-power conversion is deliberately explicit:
    ``average_demand_mw = demand_value / EIA_INTERVAL_HOURS``.  That conversion
    is valid only for the canonical one-hour EIA interval and is not persisted
    as the ambiguous raw ``demand_value`` field.
    """

    source: str
    dataset: str
    period_utc: datetime
    respondent: str
    demand_type: str
    demand_value: Decimal
    demand_unit: str
    source_request_fingerprint: str
    source_raw_payload_sha256: str
    sanitized_payload_sha256: str
    ingested_at_utc: datetime
    bronze_uri: str
    quality_status: Literal["valid"] = "valid"
    quality_flags: tuple[str, ...] = ()

    @property
    def average_demand_mw(self) -> Decimal:
        """Return explicit average MW for this one-hour MWh interval.

        Silver preserves the raw EIA value and its authoritative unit. Gold
        feature engineering may use this property only after this contract has
        validated the canonical ``megawatthours`` unit.
        """

        if self.demand_unit != EIA_HOURLY_ENERGY_UNIT:
            raise SilverValidationError("Average MW conversion requires EIA megawatthours for a one-hour interval.")
        return self.demand_value / EIA_INTERVAL_HOURS


@dataclass(frozen=True)
class WeatherSilverRecord:
    """One validated city/hour weather observation, kept separate by dataset type."""

    source: str
    dataset: Literal["historical-weather", "forecast"]
    city: str
    requested_latitude: float
    requested_longitude: float
    provider_latitude: float
    provider_longitude: float
    provider_coordinate_distance_km: float
    observed_at_utc: datetime
    temperature_2m: float
    relative_humidity_2m: float
    wind_speed_10m: float
    temperature_unit: str
    relative_humidity_unit: str
    wind_speed_unit: str
    source_request_fingerprint: str
    source_raw_payload_sha256: str
    sanitized_payload_sha256: str
    ingested_at_utc: datetime
    bronze_uri: str
    quality_status: Literal["valid"] = "valid"
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class IssuedForecastWeatherSilverRecord:
    """One city/valid-time row from a named ECMWF IFS Single Runs capture.

    This is separate from retrieval-time ``forecast`` weather: the business
    key includes the requested model initialization, and availability remains
    an explicit conservative run-plus-six-hours assumption.
    """

    source: str
    dataset: Literal["issued-forecast-weather"]
    city: str
    requested_latitude: float
    requested_longitude: float
    provider_latitude: float
    provider_longitude: float
    provider_coordinate_distance_km: float
    valid_time_utc: datetime
    weather_model: str
    weather_model_run_utc: datetime
    weather_available_at_utc: datetime
    retrieval_time_utc: datetime
    availability_basis: str
    forecast_lead_hours: int
    temperature_2m: float
    relative_humidity_2m: float
    apparent_temperature: float
    wind_speed_10m: float
    temperature_unit: str
    relative_humidity_unit: str
    apparent_temperature_unit: str
    wind_speed_unit: str
    source_request_fingerprint: str
    source_raw_payload_sha256: str
    sanitized_payload_sha256: str
    ingested_at_utc: datetime
    bronze_uri: str
    quality_status: Literal["valid"] = "valid"
    quality_flags: tuple[str, ...] = ()


@dataclass(frozen=True)
class SilverDatasets:
    """Deduplicated Silver datasets; forecasts and observed weather never mix."""

    demand: tuple[DemandSilverRecord, ...]
    historical_weather: tuple[WeatherSilverRecord, ...]
    forecast_weather: tuple[WeatherSilverRecord, ...]
    issued_forecast_weather: tuple[IssuedForecastWeatherSilverRecord, ...]

    def as_rows(self) -> dict[str, list[dict[str, object]]]:
        """Return Spark-friendly values while retaining UTC-aware datetimes."""

        return {
            "demand": [_spark_row(record) for record in self.demand],
            "historical_weather": [_spark_row(record) for record in self.historical_weather],
            "forecast_weather": [_spark_row(record) for record in self.forecast_weather],
            "issued_forecast_weather": [_spark_row(record) for record in self.issued_forecast_weather],
        }


@dataclass(frozen=True)
class ValidatedBronzeRecords:
    """Validated source rows before deterministic Silver deduplication.

    The coverage audit consumes these rows so repeated captures and source
    revisions cannot disappear silently during the normal Silver selection.
    """

    demand: tuple[DemandSilverRecord, ...]
    historical_weather: tuple[WeatherSilverRecord, ...]
    forecast_weather: tuple[WeatherSilverRecord, ...]
    issued_forecast_weather: tuple[IssuedForecastWeatherSilverRecord, ...]


def _spark_row(record: DemandSilverRecord | WeatherSilverRecord | IssuedForecastWeatherSilverRecord) -> dict[str, object]:
    """Normalize immutable record values for the explicit Spark schemas."""

    row = asdict(record)
    row["quality_flags"] = list(record.quality_flags)
    return row


def load_local_bronze_envelopes(path: Path) -> tuple[BronzeEnvelopeInput, ...]:
    """Read local ``.json`` Bronze envelopes without any S3 or network access."""

    if not path.exists():
        raise SilverValidationError("Local Bronze input path does not exist.")
    files = (path,) if path.is_file() else tuple(sorted(path.rglob("*.json")))
    if not files:
        raise SilverValidationError("Local Bronze input contains no JSON envelope files.")
    envelopes: list[BronzeEnvelopeInput] = []
    for file_path in files:
        try:
            decoded = json.loads(file_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError):
            raise SilverValidationError("A local Bronze envelope is not readable UTF-8 JSON.") from None
        if not isinstance(decoded, Mapping):
            raise SilverValidationError("A local Bronze envelope must be a JSON object.")
        envelopes.append(BronzeEnvelopeInput(uri=file_path.resolve().as_uri(), envelope=decoded))
    return tuple(envelopes)


def load_verified_ercot_parquet(path: Path) -> tuple[DemandSilverRecord, ...]:
    """Adapt a verified local EIA Parquet extract to the reviewed Silver contract.

    The parquet's SHA-256 is retained as file-level provenance because its
    original per-request EIA Bronze envelopes are not available locally. This
    adapter does not invent raw-request provenance or fill missing hours.
    """

    if not path.is_file():
        raise SilverValidationError("Verified ERCOT Parquet input does not exist.")
    try:
        import pyarrow.parquet as pq
    except ImportError:
        raise SilverValidationError("pyarrow is required to read the verified ERCOT Parquet input.") from None
    table = pq.read_table(path)
    required = ("period", "respondent", "type", "value", "value-units")
    if any(name not in table.column_names for name in required):
        raise SilverValidationError("Verified ERCOT Parquet is missing a required EIA column.")
    with path.open("rb") as stream:
        file_sha256 = file_digest(stream, "sha256").hexdigest()
    source_fingerprint = sha256(
        canonical_json_bytes(
            {
                "adapter": "verified_ercot_parquet_v1",
                "file_sha256": file_sha256,
                "source": "eia",
                "dataset": EIA_DATASET,
            }
        )
    ).hexdigest()
    ingested_at = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    bronze_uri = path.resolve().as_uri()
    records: list[DemandSilverRecord] = []
    seen: set[datetime] = set()
    for row in table.select(required).to_pylist():
        period = _parquet_utc_hour(row.get("period"))
        if period in seen:
            raise SilverValidationError("Verified ERCOT Parquet has duplicate UTC demand timestamps.")
        seen.add(period)
        respondent = _parquet_text(row.get("respondent"), "respondent")
        demand_type = _parquet_text(row.get("type"), "type")
        unit = _parquet_text(row.get("value-units"), "value-units")
        if respondent != EIA_RESPONDENT or demand_type != EIA_DEMAND_TYPE:
            raise SilverValidationError("Verified ERCOT Parquet must remain ERCO respondent and D demand type.")
        if unit != EIA_HOURLY_ENERGY_UNIT:
            raise SilverValidationError("Verified ERCOT Parquet must retain EIA megawatthours units.")
        records.append(
            DemandSilverRecord(
                source="eia",
                dataset=EIA_DATASET,
                period_utc=period,
                respondent=respondent,
                demand_type=demand_type,
                demand_value=_non_negative_decimal(row.get("value"), "EIA value"),
                demand_unit=unit,
                source_request_fingerprint=source_fingerprint,
                source_raw_payload_sha256=file_sha256,
                sanitized_payload_sha256=file_sha256,
                ingested_at_utc=ingested_at,
                bronze_uri=bronze_uri,
            )
        )
    if not records:
        raise SilverValidationError("Verified ERCOT Parquet contains no demand rows.")
    return tuple(sorted(records, key=lambda record: record.period_utc))


def transform_bronze_envelopes(envelopes: Iterable[BronzeEnvelopeInput]) -> SilverDatasets:
    """Validate, normalize, and deterministically deduplicate Bronze envelope records."""

    records = validated_bronze_records(envelopes)
    return SilverDatasets(
        demand=tuple(_deduplicate_demand(list(records.demand))),
        historical_weather=tuple(_deduplicate_weather(list(records.historical_weather))),
        forecast_weather=tuple(_deduplicate_weather(list(records.forecast_weather))),
        issued_forecast_weather=tuple(_deduplicate_issued_weather(list(records.issued_forecast_weather))),
    )


def validated_bronze_records(envelopes: Iterable[BronzeEnvelopeInput]) -> ValidatedBronzeRecords:
    """Return validated records without hiding duplicate captures or revisions."""

    demand: list[DemandSilverRecord] = []
    historical_weather: list[WeatherSilverRecord] = []
    forecast_weather: list[WeatherSilverRecord] = []
    issued_forecast_weather: list[IssuedForecastWeatherSilverRecord] = []
    for envelope_input in envelopes:
        context = _envelope_context(envelope_input)
        if context.source == "eia" and context.dataset == EIA_DATASET:
            demand.extend(_demand_records(envelope_input, context))
        elif context.source == "open_meteo" and context.dataset in {
            WEATHER_HISTORICAL_DATASET,
            WEATHER_FORECAST_DATASET,
            WEATHER_ISSUED_IFS_DATASET,
        }:
            records = _weather_records(envelope_input, context)
            if context.dataset == WEATHER_HISTORICAL_DATASET:
                historical_weather.extend(records)
            elif context.dataset == WEATHER_ISSUED_IFS_DATASET:
                issued_forecast_weather.extend(records)
            else:
                forecast_weather.extend(records)
        else:
            raise SilverValidationError("Bronze envelope source and dataset are not supported by the Silver contract.")
    return ValidatedBronzeRecords(
        demand=tuple(demand),
        historical_weather=tuple(historical_weather),
        forecast_weather=tuple(forecast_weather),
        issued_forecast_weather=tuple(issued_forecast_weather),
    )


@dataclass(frozen=True)
class _EnvelopeContext:
    source: str
    dataset: str
    ingested_at_utc: datetime
    source_range_start: datetime
    source_range_end: datetime
    request_fingerprint: str
    raw_payload_sha256: str
    sanitized_payload_sha256: str
    units: Mapping[str, str]
    source_payload: Mapping[str, object]
    request_parameters: Mapping[str, object]


def _envelope_context(envelope_input: BronzeEnvelopeInput) -> _EnvelopeContext:
    envelope = envelope_input.envelope
    if envelope.get("schema_version") != BRONZE_SCHEMA_VERSION:
        raise SilverValidationError("Bronze envelope schema version is unsupported.")
    source_payload = _required_mapping(envelope, "source_payload")
    capture = _required_mapping(envelope, "capture")
    source_range = _required_mapping(envelope, "source_time_range_utc")
    sanitized_hash = _required_sha256(envelope, "sanitized_payload_sha256")
    if sanitized_hash != sha256(canonical_json_bytes(source_payload)).hexdigest():
        raise SilverValidationError("Bronze envelope sanitized payload hash does not match source payload.")
    return _EnvelopeContext(
        source=_required_text(envelope, "source"),
        dataset=_required_text(envelope, "dataset"),
        ingested_at_utc=_parse_utc(_required_text(envelope, "ingested_at_utc"), "ingested_at_utc"),
        source_range_start=_parse_utc(_required_text(source_range, "start"), "source_time_range_utc.start"),
        source_range_end=_parse_utc(_required_text(source_range, "end"), "source_time_range_utc.end"),
        request_fingerprint=_required_sha256(envelope, "request_fingerprint"),
        raw_payload_sha256=_required_sha256(capture, "raw_payload_sha256"),
        sanitized_payload_sha256=sanitized_hash,
        units=_string_mapping(envelope.get("units"), "Bronze envelope units"),
        source_payload=source_payload,
        request_parameters=_required_mapping(capture, "request_parameters"),
    )


def _demand_records(envelope_input: BronzeEnvelopeInput, context: _EnvelopeContext) -> list[DemandSilverRecord]:
    response = _required_mapping(context.source_payload, "response")
    raw_records = response.get("data")
    if not isinstance(raw_records, list) or not raw_records:
        raise SilverValidationError("EIA Bronze response.data must be a non-empty list.")
    records: list[DemandSilverRecord] = []
    timestamps: list[datetime] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            raise SilverValidationError("EIA Bronze response.data entries must be objects.")
        respondent = _required_text(raw_record, "respondent")
        demand_type = _required_text(raw_record, "type")
        if respondent != EIA_RESPONDENT or demand_type != EIA_DEMAND_TYPE:
            raise SilverValidationError("EIA Silver records must remain ERCOT respondent ERCO and demand type D.")
        period = _parse_eia_period(_required_text(raw_record, "period"))
        timestamps.append(period)
        unit = _required_text(raw_record, "value-units")
        if context.units.get("value") != unit:
            raise SilverValidationError("EIA Bronze units do not match the authoritative record value-units.")
        if unit != EIA_HOURLY_ENERGY_UNIT:
            raise SilverValidationError("EIA Silver demand unit must remain the authoritative megawatthours contract.")
        records.append(
            DemandSilverRecord(
                source=context.source,
                dataset=context.dataset,
                period_utc=period,
                respondent=respondent,
                demand_type=demand_type,
                demand_value=_non_negative_decimal(raw_record.get("value"), "EIA value"),
                demand_unit=unit,
                source_request_fingerprint=context.request_fingerprint,
                source_raw_payload_sha256=context.raw_payload_sha256,
                sanitized_payload_sha256=context.sanitized_payload_sha256,
                ingested_at_utc=context.ingested_at_utc,
                bronze_uri=envelope_input.uri,
            )
        )
    _validate_hourly_range(timestamps, context.source_range_start, context.source_range_end, "EIA")
    return records


def _weather_records(
    envelope_input: BronzeEnvelopeInput, context: _EnvelopeContext
) -> list[WeatherSilverRecord] | list[IssuedForecastWeatherSilverRecord]:
    if context.dataset == WEATHER_ISSUED_IFS_DATASET:
        return _issued_ifs_weather_records(envelope_input, context)
    hourly = _required_mapping(context.source_payload, "hourly")
    location = _weather_location(context.dataset, context.request_parameters, context.source_payload)
    raw_times = hourly.get("time")
    if not isinstance(raw_times, list) or not raw_times:
        raise SilverValidationError("Open-Meteo Bronze hourly.time must be a non-empty list.")
    timestamps = [_unix_hour(value) for value in raw_times]
    values = {field: hourly.get(field) for field in WEATHER_FIELDS}
    for field, field_values in values.items():
        if not isinstance(field_values, list) or len(field_values) != len(timestamps):
            raise SilverValidationError(f"Open-Meteo Bronze hourly {field} must align with hourly.time.")
        if any(value is None for value in field_values):
            raise SilverValidationError(f"Open-Meteo Bronze hourly {field} must not contain missing values.")
        if field not in context.units:
            raise SilverValidationError(f"Open-Meteo Bronze units are missing {field}.")
    _validate_hourly_range(timestamps, context.source_range_start, context.source_range_end, "Open-Meteo")
    return [
        WeatherSilverRecord(
            source=context.source,
            dataset=context.dataset,  # type: ignore[arg-type]
            city=location.city,
            requested_latitude=location.requested_latitude,
            requested_longitude=location.requested_longitude,
            provider_latitude=location.provider_latitude,
            provider_longitude=location.provider_longitude,
            provider_coordinate_distance_km=location.provider_coordinate_distance_km,
            observed_at_utc=timestamp,
            temperature_2m=_finite_float(values["temperature_2m"][index], "temperature_2m"),  # type: ignore[index]
            relative_humidity_2m=_finite_float(values["relative_humidity_2m"][index], "relative_humidity_2m"),  # type: ignore[index]
            wind_speed_10m=_finite_float(values["wind_speed_10m"][index], "wind_speed_10m"),  # type: ignore[index]
            temperature_unit=context.units["temperature_2m"],
            relative_humidity_unit=context.units["relative_humidity_2m"],
            wind_speed_unit=context.units["wind_speed_10m"],
            source_request_fingerprint=context.request_fingerprint,
            source_raw_payload_sha256=context.raw_payload_sha256,
            sanitized_payload_sha256=context.sanitized_payload_sha256,
            ingested_at_utc=context.ingested_at_utc,
            bronze_uri=envelope_input.uri,
        )
        for index, timestamp in enumerate(timestamps)
    ]


def _issued_ifs_weather_records(
    envelope_input: BronzeEnvelopeInput, context: _EnvelopeContext
) -> list[IssuedForecastWeatherSilverRecord]:
    """Validate the model-run contract carried by a Single Runs Bronze object."""

    hourly = _required_mapping(context.source_payload, "hourly")
    location = _weather_location(context.dataset, context.request_parameters, context.source_payload)
    run_text = _required_text(context.request_parameters, "run")
    try:
        run = datetime.strptime(run_text, "%Y-%m-%dT%H:%M").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SilverValidationError("Issued IFS Bronze run must use the minute-precision UTC request contract.") from None
    if run.hour != 0:
        raise SilverValidationError("Issued IFS Bronze run must be 00:00 UTC.")
    if context.request_parameters.get("models") != "ecmwf_ifs":
        raise SilverValidationError("Issued weather Bronze records must identify the ECMWF IFS model.")
    raw_times = hourly.get("time")
    if not isinstance(raw_times, list) or not raw_times:
        raise SilverValidationError("Issued IFS Bronze hourly.time must be a non-empty list.")
    timestamps = [_unix_hour(value) for value in raw_times]
    required = ("temperature_2m", "relative_humidity_2m", "apparent_temperature", "wind_speed_10m")
    values = {field: hourly.get(field) for field in required}
    for field, field_values in values.items():
        if not isinstance(field_values, list) or len(field_values) != len(timestamps):
            raise SilverValidationError(f"Issued IFS Bronze hourly {field} must align with hourly.time.")
        if any(value is None for value in field_values) or field not in context.units:
            raise SilverValidationError(f"Issued IFS Bronze requires complete {field} values and units.")
    _validate_hourly_range(timestamps, context.source_range_start, context.source_range_end, "Issued IFS")
    if timestamps[0] != run:
        raise SilverValidationError("Issued IFS Bronze valid time must begin at the requested model run.")
    available = run + timedelta(hours=6)
    return [
        IssuedForecastWeatherSilverRecord(
            source=context.source,
            dataset=WEATHER_ISSUED_IFS_DATASET,
            city=location.city,
            requested_latitude=location.requested_latitude,
            requested_longitude=location.requested_longitude,
            provider_latitude=location.provider_latitude,
            provider_longitude=location.provider_longitude,
            provider_coordinate_distance_km=location.provider_coordinate_distance_km,
            valid_time_utc=timestamp,
            weather_model="ecmwf_ifs",
            weather_model_run_utc=run,
            weather_available_at_utc=available,
            retrieval_time_utc=context.ingested_at_utc,
            availability_basis="single_runs_model_init_plus_documented_global_model_max_processing_lag",
            forecast_lead_hours=int((timestamp - run).total_seconds() / 3600),
            temperature_2m=_finite_float(values["temperature_2m"][index], "temperature_2m"),  # type: ignore[index]
            relative_humidity_2m=_finite_float(values["relative_humidity_2m"][index], "relative_humidity_2m"),  # type: ignore[index]
            apparent_temperature=_finite_float(values["apparent_temperature"][index], "apparent_temperature"),  # type: ignore[index]
            wind_speed_10m=_finite_float(values["wind_speed_10m"][index], "wind_speed_10m"),  # type: ignore[index]
            temperature_unit=context.units["temperature_2m"],
            relative_humidity_unit=context.units["relative_humidity_2m"],
            apparent_temperature_unit=context.units["apparent_temperature"],
            wind_speed_unit=context.units["wind_speed_10m"],
            source_request_fingerprint=context.request_fingerprint,
            source_raw_payload_sha256=context.raw_payload_sha256,
            sanitized_payload_sha256=context.sanitized_payload_sha256,
            ingested_at_utc=context.ingested_at_utc,
            bronze_uri=envelope_input.uri,
        )
        for index, timestamp in enumerate(timestamps)
    ]


def _deduplicate_demand(records: list[DemandSilverRecord]) -> list[DemandSilverRecord]:
    selected: dict[tuple[str, str, datetime], DemandSilverRecord] = {}
    for record in records:
        key = (record.source, record.dataset, record.period_utc)
        selected[key] = max(selected.get(key, record), record, key=_provenance_rank)
    return [selected[key] for key in sorted(selected)]


def _deduplicate_weather(records: list[WeatherSilverRecord]) -> list[WeatherSilverRecord]:
    selected: dict[tuple[str, str, datetime], WeatherSilverRecord] = {}
    for record in records:
        key = (record.city, record.dataset, record.observed_at_utc)
        selected[key] = max(selected.get(key, record), record, key=_provenance_rank)
    return [selected[key] for key in sorted(selected)]


def _deduplicate_issued_weather(
    records: list[IssuedForecastWeatherSilverRecord],
) -> list[IssuedForecastWeatherSilverRecord]:
    selected: dict[tuple[str, str, datetime, datetime], IssuedForecastWeatherSilverRecord] = {}
    for record in records:
        key = (record.city, record.dataset, record.weather_model_run_utc, record.valid_time_utc)
        selected[key] = max(selected.get(key, record), record, key=_provenance_rank)
    return [selected[key] for key in sorted(selected)]


def _provenance_rank(
    record: DemandSilverRecord | WeatherSilverRecord | IssuedForecastWeatherSilverRecord,
) -> tuple[datetime, str, str, str]:
    return record.ingested_at_utc, record.source_raw_payload_sha256, record.sanitized_payload_sha256, record.bronze_uri


def _validate_hourly_range(timestamps: list[datetime], start: datetime, end: datetime, source: str) -> None:
    if not timestamps or timestamps[0] != start or timestamps[-1] != end:
        raise SilverValidationError(f"{source} Bronze source_time_range_utc does not match validated timestamps.")
    for previous, current in zip(timestamps, timestamps[1:]):
        if current - previous != timedelta(hours=1):
            raise SilverValidationError(f"{source} Bronze records contain a missing, duplicate, or non-hourly timestamp.")


@dataclass(frozen=True)
class _WeatherLocation:
    city: str
    requested_latitude: float
    requested_longitude: float
    provider_latitude: float
    provider_longitude: float
    provider_coordinate_distance_km: float


def _weather_location(
    dataset: str, parameters: Mapping[str, object], source_payload: Mapping[str, object]
) -> _WeatherLocation:
    """Identify the configured request point and validate Open-Meteo's grid cell."""

    requested_latitude = _coordinate(parameters.get("latitude"), "Open-Meteo requested latitude", latitude=True)
    requested_longitude = _coordinate(parameters.get("longitude"), "Open-Meteo requested longitude", latitude=False)
    provider_latitude = _coordinate(source_payload.get("latitude"), "Open-Meteo provider latitude", latitude=True)
    provider_longitude = _coordinate(source_payload.get("longitude"), "Open-Meteo provider longitude", latitude=False)
    matches = [
        (city, configured_latitude, configured_longitude)
        for city, configured_latitude, configured_longitude in _CONFIGURED_CITIES
        if abs(requested_latitude - configured_latitude) <= _REQUEST_COORDINATE_TOLERANCE_DEGREES
        and abs(requested_longitude - configured_longitude) <= _REQUEST_COORDINATE_TOLERANCE_DEGREES
    ]
    if len(matches) != 1:
        raise SilverValidationError("Open-Meteo request coordinates are not one configured Texas city.")
    city, configured_latitude, configured_longitude = matches[0]
    distance_km = _distance_km(
        configured_latitude,
        configured_longitude,
        provider_latitude,
        provider_longitude,
    )
    maximum_distance_km = _provider_grid_distance_limit_km(dataset, parameters)
    if distance_km > maximum_distance_km:
        raise SilverValidationError("Open-Meteo provider coordinates are too distant from the requested city.")
    return _WeatherLocation(
        city=city,
        requested_latitude=requested_latitude,
        requested_longitude=requested_longitude,
        provider_latitude=provider_latitude,
        provider_longitude=provider_longitude,
        provider_coordinate_distance_km=distance_km,
    )


def _provider_grid_distance_limit_km(dataset: str, parameters: Mapping[str, object]) -> float:
    """Select the coordinate bound only from immutable source request metadata."""

    if dataset == WEATHER_HISTORICAL_DATASET and parameters.get("models") == _OPEN_METEO_ERA5_MODEL:
        return _MAX_ERA5_PROVIDER_GRID_DISTANCE_KM
    return _MAX_PROVIDER_GRID_DISTANCE_KM


def _coordinate(value: object, field: str, *, latitude: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        raise SilverValidationError(f"{field} must be numeric.")
    try:
        coordinate = float(value)
    except ValueError:
        raise SilverValidationError(f"{field} must be numeric.") from None
    maximum = 90.0 if latitude else 180.0
    if not math.isfinite(coordinate) or not -maximum <= coordinate <= maximum:
        raise SilverValidationError(f"{field} is outside its valid geographic range.")
    return coordinate


def _distance_km(
    first_latitude: float,
    first_longitude: float,
    second_latitude: float,
    second_longitude: float,
) -> float:
    """Return the great-circle distance used for provider grid-cell validation."""

    latitude_delta = math.radians(second_latitude - first_latitude)
    longitude_delta = math.radians(second_longitude - first_longitude)
    first_latitude_radians = math.radians(first_latitude)
    second_latitude_radians = math.radians(second_latitude)
    haversine = (
        math.sin(latitude_delta / 2) ** 2
        + math.cos(first_latitude_radians)
        * math.cos(second_latitude_radians)
        * math.sin(longitude_delta / 2) ** 2
    )
    return _EARTH_RADIUS_KM * 2 * math.asin(math.sqrt(haversine))


def _parse_eia_period(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    except ValueError:
        raise SilverValidationError("EIA Bronze period must use UTC hourly format YYYY-MM-DDTHH.") from None


def _parquet_utc_hour(value: object) -> datetime:
    to_pydatetime = getattr(value, "to_pydatetime", None)
    if callable(to_pydatetime):
        value = to_pydatetime()
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise SilverValidationError("Verified ERCOT Parquet period must be a timezone-aware timestamp.")
    normalized = value.astimezone(timezone.utc)
    if normalized.minute or normalized.second or normalized.microsecond:
        raise SilverValidationError("Verified ERCOT Parquet period must align exactly to a UTC hour.")
    return normalized


def _parquet_text(value: object, field: str) -> str:
    if not isinstance(value, str) or not value:
        raise SilverValidationError(f"Verified ERCOT Parquet {field} must be a non-empty string.")
    return value


def _unix_hour(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SilverValidationError("Open-Meteo Bronze timestamps must be numeric Unix timestamps.")
    try:
        timestamp = datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        raise SilverValidationError("Open-Meteo Bronze timestamp is outside supported UTC range.") from None
    if timestamp.minute or timestamp.second or timestamp.microsecond:
        raise SilverValidationError("Open-Meteo Bronze timestamps must align to UTC hours.")
    return timestamp


def _parse_utc(value: str, field: str) -> datetime:
    if not value.endswith("Z"):
        raise SilverValidationError(f"{field} must be an ISO-8601 UTC timestamp ending in Z.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise SilverValidationError(f"{field} must be an ISO-8601 UTC timestamp.") from None
    if parsed.tzinfo is None:
        raise SilverValidationError(f"{field} must be timezone-aware.")
    return parsed.astimezone(timezone.utc)


def _non_negative_decimal(value: object, field: str) -> Decimal:
    if not isinstance(value, (str, int, float, Decimal)) or isinstance(value, bool):
        raise SilverValidationError(f"{field} must be numeric.")
    try:
        decimal_value = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise SilverValidationError(f"{field} must be numeric.") from None
    if not decimal_value.is_finite() or decimal_value < 0:
        raise SilverValidationError(f"{field} must be finite and non-negative.")
    return decimal_value


def _finite_float(value: object, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise SilverValidationError(f"Open-Meteo {field} must be numeric.")
    number = float(value)
    if not math.isfinite(number):
        raise SilverValidationError(f"Open-Meteo {field} must be finite.")
    return number


def _required_mapping(value: Mapping[str, object], field: str) -> Mapping[str, object]:
    result = value.get(field)
    if not isinstance(result, Mapping):
        raise SilverValidationError(f"{field} must be an object.")
    return result


def _string_mapping(value: object, field: str) -> Mapping[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise SilverValidationError(f"{field} must be an object or null.")
    result: dict[str, str] = {}
    for key, item in value.items():
        if not isinstance(key, str) or not isinstance(item, str) or not item.strip():
            raise SilverValidationError(f"{field} must contain non-empty string entries.")
        result[key] = item.strip()
    return result


def _required_text(value: Mapping[str, object], field: str) -> str:
    result = value.get(field)
    if not isinstance(result, str) or not result:
        raise SilverValidationError(f"{field} must be a non-empty string.")
    return result


def _required_sha256(value: Mapping[str, object], field: str) -> str:
    result = _required_text(value, field)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise SilverValidationError(f"{field} must be a lowercase SHA-256 digest.")
    return result
