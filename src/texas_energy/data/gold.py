"""Versioned, model-facing contracts for ERCOT hourly Gold features.

Gold V1 is a point-in-time feature snapshot. ``observed_*`` columns are
historical labels retained for training and evaluation; they are not asserted to
be available when issuing a future forecast. Historical observed weather is
likewise labelled explicitly so future serving cannot mistake it for a weather
forecast.
"""

from __future__ import annotations

from dataclasses import dataclass


GOLD_CONTRACT_VERSION = "1"
CANONICAL_DEMAND_UNIT = "megawatthours"
CANONICAL_WEATHER_CITIES = ("dallas", "houston", "austin", "san_antonio", "el_paso")
CANONICAL_WEATHER_UNITS = {
    "temperature_2m": "°C",
    "relative_humidity_2m": "%",
    "wind_speed_10m": "km/h",
}
HISTORICAL_OBSERVED_WEATHER = "historical_observed"


class GoldConfigurationError(ValueError):
    """Gold feature configuration is not safe or internally consistent."""


@dataclass(frozen=True)
class GoldFeatureConfig:
    """Causal lag and trailing-window configuration for one-hour observations.

    Each rolling feature uses only rows ending at ``t - 1``. The one-hour EIA
    energy value is retained in ``observed_demand_value_mwh``. Its explicitly
    named average-power derivative is ``observed_average_demand_mw``:
    ``MWh / 1 hour = average MW``.
    """

    lag_hours: tuple[int, ...] = (1, 24, 168)
    trailing_window_hours: tuple[int, ...] = (24, 168)

    def __post_init__(self) -> None:
        _validate_positive_unique_hours(self.lag_hours, "lag_hours")
        _validate_positive_unique_hours(self.trailing_window_hours, "trailing_window_hours")


def _validate_positive_unique_hours(values: tuple[int, ...], name: str) -> None:
    if not values:
        raise GoldConfigurationError(f"{name} must not be empty.")
    if any(isinstance(value, bool) or not isinstance(value, int) or value < 1 for value in values):
        raise GoldConfigurationError(f"{name} must contain positive integer hours.")
    if len(set(values)) != len(values):
        raise GoldConfigurationError(f"{name} must not contain duplicate hours.")
