"""Leakage-safe weather-forecast feature assembly for multi-horizon models."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .weather_forecasts import WeatherForecastContractError, select_as_of_weather


WEATHER_VALUE_COLUMNS = (
    "temperature_2m_c",
    "relative_humidity_pct",
    "apparent_temperature_c",
    "wind_speed_10m_kmh",
)
WEATHER_UNIT_COLUMNS = (
    "temperature_2m_unit",
    "relative_humidity_unit",
    "apparent_temperature_unit",
    "wind_speed_10m_unit",
)
EXPECTED_WEATHER_UNITS = {
    "temperature_2m_unit": "°C",
    "relative_humidity_unit": "%",
    "apparent_temperature_unit": "°C",
    "wind_speed_10m_unit": "km/h",
}


@dataclass(frozen=True)
class PointInTimeWeatherFeatures:
    """Wide model features plus the exact long-form source rows selected."""

    features: pd.DataFrame
    selected_weather: pd.DataFrame


def build_point_in_time_weather_features(
    forecast_rows: pd.DataFrame,
    weather_forecasts: pd.DataFrame,
    *,
    expected_cities: tuple[str, ...],
) -> PointInTimeWeatherFeatures:
    """Pivot only forecasts available by each issuance into model features.

    ``forecast_rows`` must contain one row for every desired origin/target
    pair.  It may include horizon and model identifiers; they are preserved.
    A city-specific weather feature is selected from the latest documented
    provider availability at or before the forecast origin.  Observed weather
    at a target timestamp is never accepted as a substitute.
    """

    if not expected_cities or len(set(expected_cities)) != len(expected_cities):
        raise WeatherForecastContractError("expected_cities must be a non-empty, unique ordered tuple.")
    required = {"forecast_origin_utc", "target_timestamp_utc"}
    if missing := required.difference(forecast_rows.columns):
        raise WeatherForecastContractError(f"Forecast rows lack columns: {sorted(missing)}")
    base = forecast_rows.copy().reset_index(drop=True)
    base["_feature_row_id"] = range(len(base))
    if base.duplicated(["forecast_origin_utc", "target_timestamp_utc"]).any():
        raise WeatherForecastContractError("Forecast rows contain duplicate origin/target pairs.")
    weather_cities = set(weather_forecasts.get("city", pd.Series(dtype=str)).dropna())
    missing_cities = set(expected_cities).difference(weather_cities)
    if missing_cities:
        raise WeatherForecastContractError(f"Weather forecasts lack expected cities: {sorted(missing_cities)}")
    if missing_units := set(WEATHER_UNIT_COLUMNS).difference(weather_forecasts.columns):
        raise WeatherForecastContractError(
            f"Weather forecasts lack explicit unit lineage: {sorted(missing_units)}"
        )
    for column, expected in EXPECTED_WEATHER_UNITS.items():
        if not weather_forecasts[column].eq(expected).all():
            raise WeatherForecastContractError(f"Weather forecast unit {column} must be {expected!r}.")
    requests = base.merge(pd.DataFrame({"city": list(expected_cities)}), how="cross")
    selected = select_as_of_weather(requests, weather_forecasts)
    counts = selected.groupby("_feature_row_id")["city"].nunique()
    if len(counts) != len(base) or not counts.eq(len(expected_cities)).all():
        raise WeatherForecastContractError("Every forecast origin/target must have exactly one weather row per city.")
    if selected.duplicated(["_feature_row_id", "city"]).any():
        raise WeatherForecastContractError("Point-in-time weather selection is not unique per city.")
    for column in WEATHER_VALUE_COLUMNS:
        if selected[column].isna().any():
            raise WeatherForecastContractError(f"Selected weather has null {column} values.")
    wide = selected.pivot(index="_feature_row_id", columns="city", values=list(WEATHER_VALUE_COLUMNS))
    wide.columns = [f"weather_{city}_{variable}" for variable, city in wide.columns]
    wide = wide.reindex(
        columns=[f"weather_{city}_{variable}" for city in expected_cities for variable in WEATHER_VALUE_COLUMNS]
    )
    result = base.merge(wide.reset_index(), on="_feature_row_id", validate="one_to_one")
    feature_sort_columns = ["forecast_origin_utc", "target_timestamp_utc"]
    if "horizon_hours" in result.columns:
        feature_sort_columns.append("horizon_hours")
    return PointInTimeWeatherFeatures(
        features=result.drop(columns="_feature_row_id").sort_values(feature_sort_columns).reset_index(drop=True),
        selected_weather=selected.sort_values(["forecast_origin_utc", "target_timestamp_utc", "city"]).reset_index(drop=True),
    )
