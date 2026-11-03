"""Open-Meteo contracts for the five Texas weather reference locations."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone
from enum import Enum

from .errors import ContractError
from .http import RetryingJsonClient
from .raw import RawCapture, utc_now


OPEN_METEO_HISTORICAL_ENDPOINT = "https://archive-api.open-meteo.com/v1/archive"
OPEN_METEO_FORECAST_ENDPOINT = "https://api.open-meteo.com/v1/forecast"
OPEN_METEO_SINGLE_RUNS_ENDPOINT = "https://single-runs-api.open-meteo.com/v1/forecast"
# Pin the archive model so a multi-year replay has a reproducible request
# identity. ERA5 is the long-range archive model selected for the 2020--2025
# historical plan; the parameter is deliberately preserved in RawCapture's
# safe request metadata and therefore its request fingerprint.
HISTORICAL_WEATHER_MODEL = "era5"
HISTORICAL_HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "wind_speed_10m",
)
FORECAST_HOURLY_FIELDS = HISTORICAL_HOURLY_FIELDS
# This is deliberately a different source contract from the current-forecast
# endpoint.  Weather-aware serving requires an explicit ECMWF IFS model run,
# including apparent temperature, rather than whatever provider revision is
# current at Lambda invocation time.
ISSUED_IFS_HOURLY_FIELDS = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "wind_speed_10m",
)
ISSUED_IFS_MODEL = "ecmwf_ifs"
ISSUED_IFS_DATASET = "issued-forecast-weather"


class TexasCity(Enum):
    """The reference locations already used by the project notebooks."""

    AUSTIN = ("austin", 30.2672, -97.7431)
    DALLAS = ("dallas", 32.7767, -96.7970)
    EL_PASO = ("el_paso", 31.7619, -106.4850)
    HOUSTON = ("houston", 29.7604, -95.3698)
    SAN_ANTONIO = ("san_antonio", 29.4241, -98.4936)

    @property
    def slug(self) -> str:
        return self.value[0]

    @property
    def latitude(self) -> float:
        return self.value[1]

    @property
    def longitude(self) -> float:
        return self.value[2]


@dataclass(frozen=True)
class WeatherResponse:
    """One city/source response with validated, UTC-normalized hourly times."""

    city: TexasCity
    dataset: str
    fields: tuple[str, ...]
    hourly_times_utc: tuple[datetime, ...]
    capture: RawCapture
    hourly_units: Mapping[str, str] = field(default_factory=dict)
    missing_hourly_value_counts: Mapping[str, int] = field(default_factory=dict)


class WeatherClient:
    """Request explicit weather fields while preserving the unmodified payload."""

    def __init__(self, http: RetryingJsonClient, *, retrieved_at=utc_now) -> None:
        self._http = http
        self._retrieved_at = retrieved_at

    def fetch_historical(
        self,
        city: TexasCity,
        *,
        start_date: date,
        end_date: date,
        fields: tuple[str, ...] = HISTORICAL_HOURLY_FIELDS,
    ) -> WeatherResponse:
        if end_date < start_date:
            raise ValueError("end_date must not precede start_date.")
        fields = _validate_requested_fields(fields, HISTORICAL_HOURLY_FIELDS)
        parameters = _historical_parameters(city, start_date, end_date, fields)
        response = self._fetch(
            endpoint=OPEN_METEO_HISTORICAL_ENDPOINT,
            dataset="historical-weather",
            city=city,
            parameters=parameters,
            fields=fields,
            allow_missing_hourly_values=True,
        )
        _validate_historical_time_range(response.hourly_times_utc, start_date, end_date)
        return response

    def fetch_forecast(
        self,
        city: TexasCity,
        *,
        forecast_hours: int = 48,
        fields: tuple[str, ...] = FORECAST_HOURLY_FIELDS,
    ) -> WeatherResponse:
        if forecast_hours < 1:
            raise ValueError("forecast_hours must be positive.")
        fields = _validate_requested_fields(fields, FORECAST_HOURLY_FIELDS)
        parameters = _forecast_parameters(city, forecast_hours, fields)
        requested_at = self._retrieved_at()
        response = self._fetch(
            endpoint=OPEN_METEO_FORECAST_ENDPOINT,
            dataset="forecast",
            city=city,
            parameters=parameters,
            fields=fields,
        )
        _validate_forecast_time_range(response.hourly_times_utc, requested_at, forecast_hours)
        return response

    def fetch_issued_ifs(
        self,
        city: TexasCity,
        *,
        model_run_utc: datetime,
        forecast_hours: int = 48,
        fields: tuple[str, ...] = ISSUED_IFS_HOURLY_FIELDS,
    ) -> WeatherResponse:
        """Capture one explicit ECMWF IFS Single Runs response.

        The run initialization and the conservative availability policy are
        retained in immutable request metadata.  They are not substituted with
        Lambda retrieval time and therefore can support a later point-in-time
        forecast-weather join.
        """

        if forecast_hours < 1 or forecast_hours > 48:
            raise ValueError("forecast_hours must be between one and 48.")
        run = _validate_ifs_run(model_run_utc)
        fields = _validate_requested_fields(fields, ISSUED_IFS_HOURLY_FIELDS)
        parameters = _issued_ifs_parameters(city, run, forecast_hours, fields)
        response = self._fetch(
            endpoint=OPEN_METEO_SINGLE_RUNS_ENDPOINT,
            dataset=ISSUED_IFS_DATASET,
            city=city,
            parameters=parameters,
            fields=fields,
        )
        _validate_issued_ifs_time_range(response.hourly_times_utc, run, forecast_hours)
        return response

    def _fetch(
        self,
        *,
        endpoint: str,
        dataset: str,
        city: TexasCity,
        parameters: Mapping[str, str],
        fields: tuple[str, ...],
        allow_missing_hourly_values: bool = False,
    ) -> WeatherResponse:
        # The transport's Linux hard deadline bounds DNS/TLS/read; retain an
        # explicit shorter socket timeout for portability.
        json_response = self._http.get_json(endpoint, parameters, timeout_seconds=20.0)
        timestamps, hourly_units, missing_hourly_value_counts = _validate_weather_payload(
            json_response.payload,
            fields,
            allow_missing_hourly_values=allow_missing_hourly_values,
        )
        capture = RawCapture(
            source="open_meteo",
            dataset=dataset,
            endpoint=endpoint,
            request_parameters=parameters,
            raw_payload=json_response.response.body,
            retrieved_at=self._retrieved_at(),
            status_code=json_response.response.status_code,
            response_headers=json_response.response.headers,
        )
        return WeatherResponse(
            city,
            dataset,
            fields,
            timestamps,
            capture,
            hourly_units,
            missing_hourly_value_counts,
        )


def _historical_parameters(
    city: TexasCity, start_date: date, end_date: date, fields: tuple[str, ...]
) -> dict[str, str]:
    return {
        "latitude": str(city.latitude),
        "longitude": str(city.longitude),
        "start_date": start_date.isoformat(),
        "end_date": end_date.isoformat(),
        "hourly": ",".join(fields),
        "timezone": "GMT",
        "timeformat": "unixtime",
        "models": HISTORICAL_WEATHER_MODEL,
    }


def _forecast_parameters(city: TexasCity, forecast_hours: int, fields: tuple[str, ...]) -> dict[str, str]:
    return {
        "latitude": str(city.latitude),
        "longitude": str(city.longitude),
        "hourly": ",".join(fields),
        "timezone": "GMT",
        "timeformat": "unixtime",
        "forecast_hours": str(forecast_hours),
    }


def _issued_ifs_parameters(
    city: TexasCity, model_run_utc: datetime, forecast_hours: int, fields: tuple[str, ...]
) -> dict[str, str]:
    return {
        "latitude": str(city.latitude),
        "longitude": str(city.longitude),
        "hourly": ",".join(fields),
        "timezone": "GMT",
        "timeformat": "unixtime",
        "models": ISSUED_IFS_MODEL,
        # Open-Meteo Single Runs accepts the documented minute-precision form;
        # retain the true UTC run separately as request metadata.
        "run": model_run_utc.strftime("%Y-%m-%dT%H:%M"),
        "forecast_hours": str(forecast_hours),
    }


def _validate_weather_payload(
    payload: Mapping[str, object],
    fields: tuple[str, ...],
    *,
    allow_missing_hourly_values: bool = False,
) -> tuple[tuple[datetime, ...], dict[str, str], dict[str, int]]:
    if payload.get("utc_offset_seconds") != 0:
        raise ContractError("Open-Meteo UTC contract requires utc_offset_seconds to equal zero.")
    hourly = payload.get("hourly")
    hourly_units = payload.get("hourly_units")
    if not isinstance(hourly, Mapping) or not isinstance(hourly_units, Mapping):
        raise ContractError("Open-Meteo response must contain hourly and hourly_units objects.")
    raw_times = hourly.get("time")
    if not isinstance(raw_times, list):
        raise ContractError("Open-Meteo hourly.time must be a list.")
    if not raw_times:
        raise ContractError("Open-Meteo hourly.time must not be empty.")

    timestamps = tuple(_parse_unix_timestamp(value) for value in raw_times)
    if tuple(sorted(timestamps)) != timestamps or len(set(timestamps)) != len(timestamps):
        raise ContractError("Open-Meteo hourly timestamps must be unique and strictly ascending.")
    if any(timestamp.minute or timestamp.second or timestamp.microsecond for timestamp in timestamps):
        raise ContractError("Open-Meteo hourly timestamps must align exactly to UTC hours.")

    validated_units: dict[str, str] = {}
    missing_hourly_value_counts: dict[str, int] = {}
    for field in fields:
        values = hourly.get(field)
        if not isinstance(values, list) or len(values) != len(timestamps):
            raise ContractError(f"Open-Meteo hourly field {field} must align with hourly.time.")
        missing_count = sum(value is None for value in values)
        if missing_count and not allow_missing_hourly_values:
            raise ContractError(f"Open-Meteo hourly field {field} must not contain missing values.")
        if missing_count:
            missing_hourly_value_counts[field] = missing_count
        unit = hourly_units.get(field)
        if not isinstance(unit, str) or not unit.strip():
            raise ContractError(f"Open-Meteo hourly_units must contain a non-empty unit for {field}.")
        validated_units[field] = unit.strip()
    return timestamps, validated_units, missing_hourly_value_counts


def _validate_requested_fields(
    fields: tuple[str, ...], allowed_fields: tuple[str, ...]
) -> tuple[str, ...]:
    if not fields:
        raise ValueError("At least one hourly weather field is required.")
    if any(not isinstance(field, str) for field in fields):
        raise ValueError("Hourly weather fields must be strings.")
    if len(set(fields)) != len(fields):
        raise ValueError("Hourly weather fields must not contain duplicates.")
    invalid_fields = [field for field in fields if field not in allowed_fields]
    if invalid_fields:
        raise ValueError(f"Unsupported hourly weather fields: {invalid_fields!r}.")
    return fields


def _validate_historical_time_range(
    timestamps: tuple[datetime, ...], start_date: date, end_date: date
) -> None:
    if any(timestamp.date() < start_date or timestamp.date() > end_date for timestamp in timestamps):
        raise ContractError("Open-Meteo historical timestamps must stay within the requested UTC date range.")


def _validate_forecast_time_range(
    timestamps: tuple[datetime, ...], requested_at: datetime, forecast_hours: int
) -> None:
    requested_hour = requested_at.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    last_allowed_hour = requested_hour + timedelta(hours=forecast_hours)
    if any(timestamp < requested_hour or timestamp > last_allowed_hour for timestamp in timestamps):
        raise ContractError("Open-Meteo forecast timestamps must stay within the requested UTC forecast window.")


def _validate_ifs_run(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise ValueError("model_run_utc must be timezone-aware.")
    run = value.astimezone(timezone.utc)
    if run.minute or run.second or run.microsecond or run.hour != 0:
        raise ValueError("ECMWF IFS Single Runs must use an exact 00:00 UTC model run.")
    return run


def _validate_issued_ifs_time_range(
    timestamps: tuple[datetime, ...], model_run_utc: datetime, forecast_hours: int
) -> None:
    expected_start = model_run_utc
    expected_end = model_run_utc + timedelta(hours=forecast_hours - 1)
    if (
        len(timestamps) != forecast_hours
        or timestamps[0] != expected_start
        or timestamps[-1] != expected_end
        or any(current - previous != timedelta(hours=1) for previous, current in zip(timestamps, timestamps[1:]))
    ):
        raise ContractError("Open-Meteo issued IFS timestamps must cover the requested contiguous model-run horizon.")


def _parse_unix_timestamp(value: object) -> datetime:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ContractError("Open-Meteo timeformat=unixtime requires numeric timestamps.")
    try:
        return datetime.fromtimestamp(value, tz=timezone.utc)
    except (OverflowError, OSError, ValueError) as error:
        raise ContractError("Open-Meteo timestamp was outside the supported UTC range.") from error
