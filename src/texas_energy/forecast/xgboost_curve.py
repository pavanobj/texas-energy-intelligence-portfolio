"""Point-in-time-valid feature construction for an XGBoost 24-hour curve."""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from .point_in_time import DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS
from .weather_features import PointInTimeWeatherFeatures, build_point_in_time_weather_features


HORIZON_HOURS = tuple(range(1, 25))
CAUSAL_CONTEXT_COLUMNS = (
    "demand_lag_1h_mwh",
    "demand_lag_24h_mwh",
    "demand_lag_168h_mwh",
    "demand_trailing_mean_24h_mwh",
    "demand_trailing_mean_168h_mwh",
)


class XGBoostCurveContractError(ValueError):
    """Input data cannot form a leakage-safe 24-hour curve example."""


@dataclass(frozen=True)
class PreparedCurveExamples:
    examples: pd.DataFrame
    excluded_no_context_or_target: int
    excluded_incomplete_context: int


def build_issued_curve_demand_examples(
    gold: pd.DataFrame,
    *,
    demand_availability_lag_hours: int = DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS,
) -> PreparedCurveExamples:
    """Generate one direct-model row for every valid issuance/horizon pair.

    At issuance ``T`` the latest usable EIA event is ``T - lag``.  All demand
    lags and rolling aggregates are taken from that *context end*, never from
    the issuance or target period.  Missing UTC event hours naturally remove
    any row needing a context or target across the gap.
    """

    if isinstance(demand_availability_lag_hours, bool) or demand_availability_lag_hours < 1:
        raise XGBoostCurveContractError("demand_availability_lag_hours must be a positive integer.")
    required = {"period_utc", "observed_average_demand_mw", *CAUSAL_CONTEXT_COLUMNS}
    if missing := required.difference(gold.columns):
        raise XGBoostCurveContractError(f"Gold input lacks columns: {sorted(missing)}")
    frame = gold.loc[:, ["period_utc", "observed_average_demand_mw", *CAUSAL_CONTEXT_COLUMNS]].copy()
    frame["period_utc"] = pd.to_datetime(frame["period_utc"], utc=True, errors="coerce")
    if frame["period_utc"].isna().any() or frame["period_utc"].duplicated().any():
        raise XGBoostCurveContractError("Gold period_utc must contain unique, valid UTC timestamps.")
    frame = frame.sort_values("period_utc").set_index("period_utc")
    candidates = pd.DataFrame({"forecast_origin_utc": frame.index}).assign(_issuance=lambda value: value["forecast_origin_utc"])
    candidates["context_end_utc"] = candidates["forecast_origin_utc"] - pd.Timedelta(hours=demand_availability_lag_hours)
    context = frame.reindex(candidates["context_end_utc"])
    context.index = candidates.index
    for column in CAUSAL_CONTEXT_COLUMNS:
        candidates[f"context_{column}"] = context[column]
    candidates["context_observed_average_demand_mw"] = context["observed_average_demand_mw"]
    expanded = candidates.loc[candidates.index.repeat(len(HORIZON_HOURS))].copy()
    expanded["horizon_hours"] = list(HORIZON_HOURS) * len(candidates)
    expanded["target_timestamp_utc"] = expanded["forecast_origin_utc"] + pd.to_timedelta(expanded["horizon_hours"], unit="h")
    expanded["target_average_demand_mw"] = expanded["target_timestamp_utc"].map(frame["observed_average_demand_mw"])
    expanded["target_utc_hour"] = expanded["target_timestamp_utc"].dt.hour.astype("int8")
    expanded["target_utc_day_of_week_sun1"] = ((expanded["target_timestamp_utc"].dt.dayofweek + 1) % 7 + 1).astype("int8")
    expanded["target_utc_month"] = expanded["target_timestamp_utc"].dt.month.astype("int8")
    expanded["target_is_weekend_utc"] = (expanded["target_timestamp_utc"].dt.dayofweek >= 5).astype("int8")
    no_context_or_target = expanded["context_observed_average_demand_mw"].isna() | expanded["target_average_demand_mw"].isna()
    feature_columns = [f"context_{column}" for column in CAUSAL_CONTEXT_COLUMNS]
    incomplete_context = expanded[feature_columns].isna().any(axis=1)
    eligible = expanded.loc[~no_context_or_target & ~incomplete_context].copy()
    if not (eligible["context_end_utc"] <= eligible["forecast_origin_utc"]).all():
        raise XGBoostCurveContractError("Demand context cannot be newer than forecast issuance.")
    return PreparedCurveExamples(
        examples=eligible.reset_index(drop=True),
        excluded_no_context_or_target=int(no_context_or_target.sum()),
        excluded_incomplete_context=int((~no_context_or_target & incomplete_context).sum()),
    )


def add_point_in_time_weather_features(
    demand_examples: pd.DataFrame,
    weather_forecasts: pd.DataFrame,
    *,
    expected_cities: tuple[str, ...],
) -> PointInTimeWeatherFeatures:
    """Add future valid-time weather only where its documented availability permits it."""

    return build_point_in_time_weather_features(
        demand_examples,
        weather_forecasts,
        expected_cities=expected_cities,
    )


def weather_aware_feature_columns(expected_cities: tuple[str, ...]) -> tuple[str, ...]:
    """Return the direct-curve XGBoost feature contract in stable order."""

    if not expected_cities or len(set(expected_cities)) != len(expected_cities):
        raise XGBoostCurveContractError("expected_cities must be a non-empty, unique ordered tuple.")
    calendar = (
        "horizon_hours",
        "target_utc_hour",
        "target_utc_day_of_week_sun1",
        "target_utc_month",
        "target_is_weekend_utc",
    )
    weather = tuple(
        f"weather_{city}_{variable}"
        for city in expected_cities
        for variable in (
            "temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh"
        )
    )
    return tuple(f"context_{column}" for column in CAUSAL_CONTEXT_COLUMNS) + calendar + weather


def fit_weather_aware_curve_xgboost(
    train: pd.DataFrame,
    validation: pd.DataFrame,
    *,
    feature_columns: tuple[str, ...],
    n_jobs: int = 4,
    n_estimators: int = 1500,
    random_state: int = 42,
):
    """Fit a direct 24-horizon model only after PIT weather coverage is proven."""

    required = {"target_average_demand_mw", *feature_columns}
    if missing := required.difference(train.columns) | required.difference(validation.columns):
        raise XGBoostCurveContractError(f"Training examples lack columns: {sorted(missing)}")
    if train.loc[:, list(feature_columns)].isna().any().any() or validation.loc[:, list(feature_columns)].isna().any().any():
        raise XGBoostCurveContractError("Weather-aware XGBoost cannot train with missing feature values.")
    try:
        from xgboost import XGBRegressor
    except ImportError:
        raise RuntimeError("Install the forecast optional dependency group to train weather-aware XGBoost.") from None
    model = XGBRegressor(
        objective="reg:squarederror", n_estimators=n_estimators, learning_rate=0.03,
        max_depth=8, min_child_weight=5, subsample=0.8, colsample_bytree=0.8,
        reg_alpha=0.0, reg_lambda=1.0, random_state=random_state, tree_method="hist",
        eval_metric="rmse", early_stopping_rounds=75, n_jobs=n_jobs,
    )
    model.fit(
        train.loc[:, list(feature_columns)], train["target_average_demand_mw"],
        eval_set=[(validation.loc[:, list(feature_columns)], validation["target_average_demand_mw"])], verbose=False,
    )
    return model
