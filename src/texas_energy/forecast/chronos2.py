"""Pure helpers for leakage-free, zero-shot Chronos-2 evaluation."""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np
import pandas as pd


CHRONOS2_MODEL_ID = "amazon/chronos-2"
DEFAULT_CONTEXT_HOURS = 2048
FORECAST_HORIZON_HOURS = 24


class Chronos2InputError(ValueError):
    """The supplied hourly target history cannot safely form a forecast."""


@dataclass(frozen=True)
class Chronos2Origin:
    """One causal forecast origin with its known history and labelled target."""

    origin_utc: pd.Timestamp
    target_utc: pd.Timestamp
    context_values: np.ndarray
    actual_average_demand_mw: float
    actual_curve_values: np.ndarray


@dataclass(frozen=True)
class Chronos2IssuedCurveOrigin:
    """One forecast issued at ``forecast_origin_utc`` with delayed demand context."""

    forecast_origin_utc: pd.Timestamp
    context_end_utc: pd.Timestamp
    context_values: np.ndarray
    target_values: np.ndarray
    demand_availability_lag_hours: int


def _hourly_index(start: pd.Timestamp, end: pd.Timestamp) -> pd.DatetimeIndex:
    return pd.date_range(start=start, end=end, freq="h", tz="UTC")


def build_origins(
    frame: pd.DataFrame,
    *,
    target_start_utc: pd.Timestamp,
    target_end_utc: pd.Timestamp,
    context_hours: int = DEFAULT_CONTEXT_HOURS,
    horizon_hours: int = FORECAST_HORIZON_HOURS,
) -> tuple[Chronos2Origin, ...]:
    """Build fully-continuous, strictly causal origins for Chronos-2.

    The target window, rather than the origin window, defines chronological
    split membership.  A missing timestamp anywhere from the first context
    value through the T+24 target makes that origin ineligible; timestamps are
    never compressed or imputed.
    """

    if context_hours < 1 or horizon_hours < 1:
        raise Chronos2InputError("context_hours and horizon_hours must be positive.")
    required = {"period_utc", "observed_average_demand_mw"}
    missing = required.difference(frame.columns)
    if missing:
        raise Chronos2InputError(f"Gold input is missing required columns: {sorted(missing)}")

    data = frame.loc[:, ["period_utc", "observed_average_demand_mw"]].copy()
    data["period_utc"] = pd.to_datetime(data["period_utc"], utc=True, errors="raise")
    data["observed_average_demand_mw"] = pd.to_numeric(
        data["observed_average_demand_mw"], errors="raise"
    )
    if data["period_utc"].duplicated().any():
        raise Chronos2InputError("Gold demand timestamps must be unique.")
    if data["observed_average_demand_mw"].isna().any():
        raise Chronos2InputError("Gold demand contains null values.")
    data = data.set_index("period_utc").sort_index()
    target_start = pd.to_datetime(target_start_utc, utc=True)
    target_end = pd.to_datetime(target_end_utc, utc=True)
    if target_end < target_start:
        raise Chronos2InputError("target_end_utc must not precede target_start_utc.")

    origins: list[Chronos2Origin] = []
    for target_utc in _hourly_index(target_start, target_end):
        origin_utc = target_utc - pd.Timedelta(hours=horizon_hours)
        context_start = origin_utc - pd.Timedelta(hours=context_hours - 1)
        required_index = _hourly_index(context_start, target_utc)
        if not required_index.isin(data.index).all():
            continue
        context = data.loc[_hourly_index(context_start, origin_utc), "observed_average_demand_mw"].to_numpy(
            dtype=np.float64
        )
        actual_curve = data.loc[_hourly_index(origin_utc + pd.Timedelta(hours=1), target_utc), "observed_average_demand_mw"].to_numpy(
            dtype=np.float64
        )
        if (
            len(context) != context_hours
            or len(actual_curve) != horizon_hours
            or not np.isfinite(context).all()
            or not np.isfinite(actual_curve).all()
        ):
            continue
        origins.append(
            Chronos2Origin(
                origin_utc=origin_utc,
                target_utc=target_utc,
                context_values=context,
                actual_average_demand_mw=float(data.loc[target_utc, "observed_average_demand_mw"]),
                actual_curve_values=actual_curve,
            )
        )
    return tuple(origins)


def contexts_to_long_frame(origins: tuple[Chronos2Origin, ...]) -> pd.DataFrame:
    """Return Chronos-2 ``predict_df`` input, containing known values only."""

    rows: list[pd.DataFrame] = []
    for index, origin in enumerate(origins):
        start = origin.origin_utc - pd.Timedelta(hours=len(origin.context_values) - 1)
        timestamps = _hourly_index(start, origin.origin_utc)
        rows.append(
            pd.DataFrame(
                {
                    "item_id": f"ercot-{index}",
                    # The official pandas API expects regular local-naive
                    # timestamps.  Values still represent UTC instants.
                    "timestamp": timestamps.tz_localize(None),
                    "target": origin.context_values,
                }
            )
        )
    if not rows:
        raise Chronos2InputError("At least one eligible origin is required.")
    return pd.concat(rows, ignore_index=True)


def build_issued_curve_origins(
    frame: pd.DataFrame,
    *,
    forecast_start_utc: pd.Timestamp,
    forecast_end_utc: pd.Timestamp,
    context_hours: int = DEFAULT_CONTEXT_HOURS,
    horizon_hours: int = FORECAST_HORIZON_HOURS,
    demand_availability_lag_hours: int = 24,
) -> tuple[Chronos2IssuedCurveOrigin, ...]:
    """Build causal 24-step curves where issuance and context end differ.

    An issued forecast at ``T`` may use demand only through ``T - lag``.  The
    model therefore emits ``lag + horizon`` steps, and only steps after T are
    returned as the 24 operational targets.  This avoids redefining a stale
    demand observation's event time as the forecast issuance time.
    """

    if context_hours < 1 or horizon_hours < 1 or demand_availability_lag_hours < 1:
        raise Chronos2InputError("context_hours, horizon_hours, and demand availability lag must be positive.")
    required = {"period_utc", "observed_average_demand_mw"}
    if missing := required.difference(frame.columns):
        raise Chronos2InputError(f"Gold input is missing required columns: {sorted(missing)}")
    data = frame.loc[:, ["period_utc", "observed_average_demand_mw"]].copy()
    data["period_utc"] = pd.to_datetime(data["period_utc"], utc=True, errors="raise")
    data["observed_average_demand_mw"] = pd.to_numeric(data["observed_average_demand_mw"], errors="raise")
    if data["period_utc"].duplicated().any() or data["observed_average_demand_mw"].isna().any():
        raise Chronos2InputError("Gold demand timestamps must be unique and demand values non-null.")
    data = data.set_index("period_utc").sort_index()
    start = pd.to_datetime(forecast_start_utc, utc=True)
    end = pd.to_datetime(forecast_end_utc, utc=True)
    if end < start:
        raise Chronos2InputError("forecast_end_utc must not precede forecast_start_utc.")
    result: list[Chronos2IssuedCurveOrigin] = []
    for issuance in _hourly_index(start, end):
        context_end = issuance - pd.Timedelta(hours=demand_availability_lag_hours)
        context_start = context_end - pd.Timedelta(hours=context_hours - 1)
        target_end = issuance + pd.Timedelta(hours=horizon_hours)
        required_index = _hourly_index(context_start, target_end)
        if not required_index.isin(data.index).all():
            continue
        context = data.loc[_hourly_index(context_start, context_end), "observed_average_demand_mw"].to_numpy(dtype=np.float64)
        target_values = data.loc[_hourly_index(issuance + pd.Timedelta(hours=1), target_end), "observed_average_demand_mw"].to_numpy(dtype=np.float64)
        if len(context) != context_hours or len(target_values) != horizon_hours:
            continue
        result.append(Chronos2IssuedCurveOrigin(issuance, context_end, context, target_values, demand_availability_lag_hours))
    return tuple(result)


def issued_curve_contexts_to_long_frame(origins: tuple[Chronos2IssuedCurveOrigin, ...]) -> pd.DataFrame:
    """Prepare only demand demonstrably available at each issuance time."""

    rows: list[pd.DataFrame] = []
    for index, origin in enumerate(origins):
        start = origin.context_end_utc - pd.Timedelta(hours=len(origin.context_values) - 1)
        rows.append(pd.DataFrame({"item_id": f"issued-ercot-{index}", "timestamp": _hourly_index(start, origin.context_end_utc).tz_localize(None), "target": origin.context_values}))
    if not rows:
        raise Chronos2InputError("At least one eligible issued curve origin is required.")
    return pd.concat(rows, ignore_index=True)


def issued_curve_predictions(
    forecast_frame: pd.DataFrame,
    origins: tuple[Chronos2IssuedCurveOrigin, ...],
) -> pd.DataFrame:
    """Extract operational horizons 1–24 after a delayed-demand context."""

    needed = {"item_id", "timestamp", "predictions"}
    if missing := needed.difference(forecast_frame.columns):
        raise Chronos2InputError(f"Chronos output is missing expected columns: {sorted(missing)}")
    output = forecast_frame.copy()
    output["timestamp"] = pd.to_datetime(output["timestamp"], utc=True, errors="raise")
    records: list[dict[str, object]] = []
    for index, origin in enumerate(origins):
        expected = _hourly_index(origin.forecast_origin_utc + pd.Timedelta(hours=1), origin.forecast_origin_utc + pd.Timedelta(hours=FORECAST_HORIZON_HOURS))
        points = output.loc[output["item_id"] == f"issued-ercot-{index}", ["timestamp", "predictions"]]
        points = points.loc[points["timestamp"].isin(expected)].sort_values("timestamp").reset_index(drop=True)
        if len(points) != FORECAST_HORIZON_HOURS or not points["timestamp"].reset_index(drop=True).equals(pd.Series(expected)):
            raise Chronos2InputError("Chronos output lacks one or more issued-forecast horizons.")
        for horizon, point in enumerate(points.itertuples(index=False), start=1):
            records.append({
                "model_id": CHRONOS2_MODEL_ID,
                "forecast_origin_utc": origin.forecast_origin_utc,
                "context_end_utc": origin.context_end_utc,
                "horizon_hours": horizon,
                "target_timestamp_utc": point.timestamp,
                "predicted_demand_mw": float(point.predictions),
                "actual_demand_mw": float(origin.target_values[horizon - 1]),
                "baseline_predicted_demand_mw": float(origin.context_values[-1]),
                "feature_as_of_utc": origin.context_end_utc,
                "demand_availability_lag_hours": origin.demand_availability_lag_hours,
            })
    return pd.DataFrame.from_records(records)


def pending_origins(
    origins: tuple[Chronos2Origin, ...], completed_target_utc: set[pd.Timestamp]
) -> tuple[Chronos2Origin, ...]:
    """Exclude only origins with a durable, exact target-timestamp result."""

    completed = {pd.to_datetime(value, utc=True) for value in completed_target_utc}
    return tuple(origin for origin in origins if origin.target_utc not in completed)


def horizon_predictions(
    forecast_frame: pd.DataFrame,
    origins: tuple[Chronos2Origin, ...],
) -> pd.DataFrame:
    """Select exactly the T+24 point prediction from each 24-step forecast."""

    needed = {"item_id", "timestamp", "predictions"}
    missing = needed.difference(forecast_frame.columns)
    if missing:
        raise Chronos2InputError(f"Chronos output is missing expected columns: {sorted(missing)}")
    output = forecast_frame.copy()
    output["timestamp"] = pd.to_datetime(output["timestamp"], utc=True, errors="raise")
    records: list[dict[str, object]] = []
    for index, origin in enumerate(origins):
        point = output.loc[
            (output["item_id"] == f"ercot-{index}") & (output["timestamp"] == origin.target_utc)
        ]
        if len(point) != 1:
            raise Chronos2InputError(
                f"Chronos output must contain exactly one T+{(origin.target_utc - origin.origin_utc).total_seconds() / 3600:g} point per origin."
            )
        records.append(
            {
                "origin_utc": origin.origin_utc,
                "target_utc": origin.target_utc,
                "actual_average_demand_mw": origin.actual_average_demand_mw,
                "chronos2_prediction_mw": float(point.iloc[0]["predictions"]),
                "seasonal_naive_prediction_mw": float(origin.context_values[-1]),
            }
        )
    return pd.DataFrame.from_records(records)


def curve_predictions(
    forecast_frame: pd.DataFrame,
    origins: tuple[Chronos2Origin, ...],
) -> pd.DataFrame:
    """Return all forecast steps emitted by Chronos-2 for every origin.

    ``horizon_predictions`` remains the historical T+24 benchmark adapter.
    This function is the Forecast Contract V2 adapter: one complete hourly
    curve for each causal origin, without reconstructing or rescaling model
    output.  The target actual is labelled only when the caller's historical
    frame made it available while building the origin.
    """

    needed = {"item_id", "timestamp", "predictions"}
    missing = needed.difference(forecast_frame.columns)
    if missing:
        raise Chronos2InputError(f"Chronos output is missing expected columns: {sorted(missing)}")
    output = forecast_frame.copy()
    output["timestamp"] = pd.to_datetime(output["timestamp"], utc=True, errors="raise")
    records: list[dict[str, object]] = []
    for index, origin in enumerate(origins):
        expected = _hourly_index(origin.origin_utc + pd.Timedelta(hours=1), origin.target_utc)
        points = output.loc[output["item_id"] == f"ercot-{index}", ["timestamp", "predictions"]]
        if points["timestamp"].duplicated().any() or not points["timestamp"].isin(expected).all():
            raise Chronos2InputError("Chronos output contains duplicate or unexpected curve timestamps.")
        if not points["timestamp"].sort_values().reset_index(drop=True).equals(pd.Series(expected)):
            raise Chronos2InputError("Chronos output must contain every hourly step through the requested horizon.")
        for point in points.itertuples(index=False):
            horizon = int((point.timestamp - origin.origin_utc).total_seconds() // 3600)
            records.append(
                {
                    "model_id": CHRONOS2_MODEL_ID,
                    "forecast_origin_utc": origin.origin_utc,
                    "horizon_hours": horizon,
                    "target_timestamp_utc": point.timestamp,
                    "predicted_demand_mw": float(point.predictions),
                    "actual_demand_mw": float(origin.actual_curve_values[horizon - 1]),
                    "feature_as_of_utc": origin.origin_utc,
                }
            )
    return pd.DataFrame.from_records(records).sort_values(
        ["forecast_origin_utc", "horizon_hours"]
    ).reset_index(drop=True)
