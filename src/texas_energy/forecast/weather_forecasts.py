"""Validated point-in-time records for issued Open-Meteo weather forecasts.

This module deliberately separates a weather forecast's valid time from when
it can be used.  It never relabels a historical observation as a forecast and
does not invent a provider run timestamp when an API supplies only a lead-time
series.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping

from dataclasses import dataclass

import pandas as pd


PREVIOUS_RUNS_SOURCE = "open_meteo_previous_runs"
SINGLE_RUNS_SOURCE = "open_meteo_single_runs"
SINGLE_RUNS_ECMWF_IFS_MODEL = "ecmwf_ifs"
PREVIOUS_DAY_2_LEAD_HOURS = 48
DEFAULT_PROVIDER_AVAILABILITY_LAG_HOURS = 12
ECMWF_IFS_DOCUMENTED_MAX_AVAILABILITY_LAG_HOURS = 6
WEATHER_VARIABLES = (
    "temperature_2m",
    "relative_humidity_2m",
    "apparent_temperature",
    "wind_speed_10m",
)


class WeatherForecastContractError(ValueError):
    """Weather forecast records cannot support a point-in-time feature join."""


@dataclass(frozen=True)
class WeatherForecastRecord:
    city: str
    valid_time_utc: pd.Timestamp
    forecast_source: str
    weather_model: str | None
    forecast_lead_hours: int
    # A Single Runs response identifies a model initialization, not a proven
    # customer-visible publication timestamp.  Keep both concepts distinct.
    weather_model_run_utc: pd.Timestamp | None
    weather_forecast_issue_utc: pd.Timestamp | None
    weather_available_at_utc: pd.Timestamp
    retrieval_time_utc: pd.Timestamp
    availability_basis: str
    temperature_2m_c: float
    temperature_2m_unit: str
    relative_humidity_pct: float
    relative_humidity_unit: str
    apparent_temperature_c: float
    apparent_temperature_unit: str
    wind_speed_10m_kmh: float
    wind_speed_10m_unit: str


def _utc(value: object, *, name: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed):
        raise WeatherForecastContractError(f"{name} must be a timezone-aware UTC timestamp.")
    return parsed


def _utc_single_run_time(value: object) -> pd.Timestamp:
    """Parse the explicit ``timeformat=unixtime`` request contract."""

    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise WeatherForecastContractError("Open-Meteo Single Runs hourly.time must contain Unix seconds.")
    parsed = pd.to_datetime(value, unit="s", utc=True, errors="coerce")
    if pd.isna(parsed):
        raise WeatherForecastContractError("Open-Meteo Single Runs hourly.time contains an invalid Unix second.")
    return parsed


def parse_previous_day2_response(
    payload: Mapping[str, object],
    *,
    city: str,
    retrieval_time_utc: object,
    weather_model: str | None = None,
    availability_lag_hours: int = DEFAULT_PROVIDER_AVAILABILITY_LAG_HOURS,
) -> tuple[WeatherForecastRecord, ...]:
    """Parse documented ``*_previous_day2`` data without inventing run time.

    Open-Meteo documents these values as forecasts made 48 hours before their
    valid time.  It does not expose the exact individual run identifier in
    this response.  Therefore ``weather_forecast_issue_utc`` remains ``None``.
    ``weather_available_at_utc`` is an explicitly conservative *assumption*:
    valid time minus 48 hours plus a configurable 12-hour provider lag.  The
    as-of join retains this basis in every row and refuses later revisions.
    """

    if not city or not city.strip():
        raise WeatherForecastContractError("city is required.")
    if isinstance(availability_lag_hours, bool) or availability_lag_hours < 0:
        raise WeatherForecastContractError("availability_lag_hours must be a non-negative integer.")
    hourly = payload.get("hourly")
    units = payload.get("hourly_units")
    if not isinstance(hourly, Mapping) or not isinstance(units, Mapping):
        raise WeatherForecastContractError("Open-Meteo response requires hourly and hourly_units objects.")
    if payload.get("utc_offset_seconds") not in (0, 0.0):
        raise WeatherForecastContractError("Open-Meteo Previous Runs response must use UTC.")
    suffix = "_previous_day2"
    keys = {variable: f"{variable}{suffix}" for variable in WEATHER_VARIABLES}
    required = ("time", *keys.values())
    if any(key not in hourly for key in required) or any(key not in units for key in required):
        raise WeatherForecastContractError("Open-Meteo response lacks a required previous-day-2 variable or unit.")
    values = {key: hourly[key] for key in required}
    if not all(isinstance(value, list) for value in values.values()):
        raise WeatherForecastContractError("Open-Meteo hourly fields must be arrays.")
    lengths = {len(value) for value in values.values()}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise WeatherForecastContractError("Open-Meteo hourly arrays must be non-empty and equal length.")
    if any(not isinstance(units[key], str) or not units[key].strip() for key in keys.values()):
        raise WeatherForecastContractError("Open-Meteo weather units must be non-empty strings.")
    retrieval = _utc(retrieval_time_utc, name="retrieval_time_utc")
    result: list[WeatherForecastRecord] = []
    for index, raw_time in enumerate(values["time"]):
        valid = _utc(raw_time, name="hourly.time")
        if valid != valid.floor("h"):
            raise WeatherForecastContractError("Open-Meteo valid timestamps must align to whole UTC hours.")
        try:
            weather_values = {variable: float(values[key][index]) for variable, key in keys.items()}
        except (TypeError, ValueError):
            raise WeatherForecastContractError("Open-Meteo weather values must be finite numbers.") from None
        if not all(pd.notna(value) and pd.api.types.is_number(value) for value in weather_values.values()):
            raise WeatherForecastContractError("Open-Meteo weather values cannot be null.")
        available = valid - pd.Timedelta(hours=PREVIOUS_DAY_2_LEAD_HOURS - availability_lag_hours)
        result.append(
            WeatherForecastRecord(
                city=city,
                valid_time_utc=valid,
                forecast_source=PREVIOUS_RUNS_SOURCE,
                weather_model=weather_model,
                forecast_lead_hours=PREVIOUS_DAY_2_LEAD_HOURS,
                weather_model_run_utc=None,
                weather_forecast_issue_utc=None,
                weather_available_at_utc=available,
                retrieval_time_utc=retrieval,
                availability_basis="documented_48h_lead_plus_conservative_provider_lag_assumption",
                temperature_2m_c=weather_values["temperature_2m"],
                temperature_2m_unit=units[keys["temperature_2m"]].strip(),
                relative_humidity_pct=weather_values["relative_humidity_2m"],
                relative_humidity_unit=units[keys["relative_humidity_2m"]].strip(),
                apparent_temperature_c=weather_values["apparent_temperature"],
                apparent_temperature_unit=units[keys["apparent_temperature"]].strip(),
                wind_speed_10m_kmh=weather_values["wind_speed_10m"],
                wind_speed_10m_unit=units[keys["wind_speed_10m"]].strip(),
            )
        )
    return tuple(result)


def parse_single_run_response(
    payload: Mapping[str, object],
    *,
    city: str,
    model_run_utc: object,
    retrieval_time_utc: object,
    weather_model: str = SINGLE_RUNS_ECMWF_IFS_MODEL,
    availability_lag_hours: int = ECMWF_IFS_DOCUMENTED_MAX_AVAILABILITY_LAG_HOURS,
) -> tuple[WeatherForecastRecord, ...]:
    """Parse an Open-Meteo Single Runs response for one explicit model run.

    ``model_run_utc`` is the requested model initialization timestamp.  It is
    retained as lineage, but is *not* relabelled as the time a forecast became
    operationally available.  Open-Meteo documents global-model processing as
    taking roughly four to six hours; the default uses that six-hour upper end
    as a conservative availability bound.  Callers can make it stricter but
    may not use a negative lag.
    """

    if not city or not city.strip():
        raise WeatherForecastContractError("city is required.")
    if not weather_model or not weather_model.strip():
        raise WeatherForecastContractError("weather_model is required.")
    if isinstance(availability_lag_hours, bool) or availability_lag_hours < 0:
        raise WeatherForecastContractError("availability_lag_hours must be a non-negative integer.")
    hourly = payload.get("hourly")
    units = payload.get("hourly_units")
    if not isinstance(hourly, Mapping) or not isinstance(units, Mapping):
        raise WeatherForecastContractError("Open-Meteo response requires hourly and hourly_units objects.")
    if payload.get("utc_offset_seconds") not in (0, 0.0):
        raise WeatherForecastContractError("Open-Meteo Single Runs response must use UTC.")
    required = ("time", *WEATHER_VARIABLES)
    if any(key not in hourly for key in required) or any(key not in units for key in required):
        raise WeatherForecastContractError("Open-Meteo response lacks a required single-run variable or unit.")
    values = {key: hourly[key] for key in required}
    if not all(isinstance(value, list) for value in values.values()):
        raise WeatherForecastContractError("Open-Meteo hourly fields must be arrays.")
    lengths = {len(value) for value in values.values()}
    if len(lengths) != 1 or not lengths or next(iter(lengths)) == 0:
        raise WeatherForecastContractError("Open-Meteo hourly arrays must be non-empty and equal length.")
    if any(not isinstance(units[key], str) or not units[key].strip() for key in WEATHER_VARIABLES):
        raise WeatherForecastContractError("Open-Meteo weather units must be non-empty strings.")
    run = _utc(model_run_utc, name="model_run_utc")
    retrieval = _utc(retrieval_time_utc, name="retrieval_time_utc")
    available = run + pd.Timedelta(hours=availability_lag_hours)
    result: list[WeatherForecastRecord] = []
    for index, raw_time in enumerate(values["time"]):
        valid = _utc_single_run_time(raw_time)
        if valid != valid.floor("h"):
            raise WeatherForecastContractError("Open-Meteo valid timestamps must align to whole UTC hours.")
        try:
            weather_values = {variable: float(values[variable][index]) for variable in WEATHER_VARIABLES}
        except (TypeError, ValueError):
            raise WeatherForecastContractError("Open-Meteo weather values must be finite numbers.") from None
        if not all(pd.notna(value) and pd.api.types.is_number(value) for value in weather_values.values()):
            raise WeatherForecastContractError("Open-Meteo weather values cannot be null.")
        lead = int((valid - run) / pd.Timedelta(hours=1))
        if lead < 0:
            raise WeatherForecastContractError("Open-Meteo Single Runs valid time precedes its requested model run.")
        result.append(
            WeatherForecastRecord(
                city=city,
                valid_time_utc=valid,
                forecast_source=SINGLE_RUNS_SOURCE,
                weather_model=weather_model,
                forecast_lead_hours=lead,
                weather_model_run_utc=run,
                weather_forecast_issue_utc=None,
                weather_available_at_utc=available,
                retrieval_time_utc=retrieval,
                availability_basis="single_runs_model_init_plus_documented_global_model_max_processing_lag",
                temperature_2m_c=weather_values["temperature_2m"],
                temperature_2m_unit=units["temperature_2m"].strip(),
                relative_humidity_pct=weather_values["relative_humidity_2m"],
                relative_humidity_unit=units["relative_humidity_2m"].strip(),
                apparent_temperature_c=weather_values["apparent_temperature"],
                apparent_temperature_unit=units["apparent_temperature"].strip(),
                wind_speed_10m_kmh=weather_values["wind_speed_10m"],
                wind_speed_10m_unit=units["wind_speed_10m"].strip(),
            )
        )
    return tuple(result)


def records_frame(records: Iterable[WeatherForecastRecord]) -> pd.DataFrame:
    """Return a stable long-form weather-forecast side-table frame."""

    frame = pd.DataFrame([record.__dict__ for record in records])
    if frame.empty:
        raise WeatherForecastContractError("At least one weather forecast record is required.")
    identity = ["city", "valid_time_utc", "forecast_source", "weather_model", "weather_model_run_utc"]
    if frame.duplicated(identity).any():
        raise WeatherForecastContractError("Weather forecast records contain duplicate city/valid-time identities.")
    return frame.sort_values(["city", "valid_time_utc"]).reset_index(drop=True)


def select_as_of_weather(
    forecast_requests: pd.DataFrame,
    weather: pd.DataFrame,
) -> pd.DataFrame:
    """Join only records demonstrably available by each forecast origin.

    ``forecast_requests`` requires city, forecast_origin_utc, and
    target_timestamp_utc.  This preserves forecast revisions as distinct data:
    a row retrieved later cannot replace an already issued historical feature.
    """

    required_requests = {"city", "forecast_origin_utc", "target_timestamp_utc"}
    required_weather = {
        "city", "valid_time_utc", "weather_available_at_utc", "retrieval_time_utc",
        "availability_basis", *(
            "temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh"
        ),
    }
    if missing := required_requests.difference(forecast_requests.columns):
        raise WeatherForecastContractError(f"Forecast requests lack columns: {sorted(missing)}")
    if missing := required_weather.difference(weather.columns):
        raise WeatherForecastContractError(f"Weather side table lacks columns: {sorted(missing)}")
    left = forecast_requests.copy()
    right = weather.copy()
    for column in ("forecast_origin_utc", "target_timestamp_utc"):
        left[column] = pd.to_datetime(left[column], utc=True, errors="raise")
    for column in ("valid_time_utc", "weather_available_at_utc", "retrieval_time_utc"):
        right[column] = pd.to_datetime(right[column], utc=True, errors="raise")
    left["_request_id"] = range(len(left))
    joined = left.merge(right, how="left", left_on=["city", "target_timestamp_utc"], right_on=["city", "valid_time_utc"])
    joined = joined.loc[
        joined["weather_available_at_utc"].notna()
        & (joined["weather_available_at_utc"] <= joined["forecast_origin_utc"])
    ].copy()
    if joined["_request_id"].nunique() != len(left):
        raise WeatherForecastContractError("A requested target hour lacks weather forecast data available at issuance.")
    # Multiple runs can validly cover one city/hour.  Select the latest model
    # run known to have been available by issuance; never use retrieval time
    # (archival collection commonly happens much later).
    joined = joined.sort_values(["_request_id", "weather_available_at_utc", "weather_model_run_utc"], na_position="first")
    selected = joined.groupby("_request_id", as_index=False, sort=False).tail(1)
    return selected.drop(columns="_request_id").sort_values(["forecast_origin_utc", "city"]).reset_index(drop=True)
