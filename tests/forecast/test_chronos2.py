from __future__ import annotations

import pandas as pd
import pytest

from texas_energy.forecast.chronos2 import (
    CHRONOS2_MODEL_ID,
    Chronos2InputError,
    build_origins,
    contexts_to_long_frame,
    curve_predictions,
    build_issued_curve_origins,
    issued_curve_contexts_to_long_frame,
    issued_curve_predictions,
    horizon_predictions,
    pending_origins,
)


def _hourly_frame(hours: int = 80) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "period_utc": pd.date_range("2024-01-01", periods=hours, freq="h", tz="UTC"),
            "observed_average_demand_mw": range(10_000, 10_000 + hours),
        }
    )


def test_chronos2_model_identity_is_official() -> None:
    assert CHRONOS2_MODEL_ID == "amazon/chronos-2"


def test_origins_use_target_window_and_require_complete_history() -> None:
    frame = _hourly_frame()
    target = pd.Timestamp("2024-01-03T12:00:00Z")
    origins = build_origins(
        frame,
        target_start_utc=target,
        target_end_utc=target,
        context_hours=24,
        horizon_hours=24,
    )
    assert len(origins) == 1
    assert origins[0].origin_utc == target - pd.Timedelta(hours=24)
    assert origins[0].context_values.shape == (24,)
    assert origins[0].context_values[-1] == frame.loc[frame.period_utc == origins[0].origin_utc, "observed_average_demand_mw"].iloc[0]


def test_missing_hour_excludes_context_instead_of_compressing_time() -> None:
    frame = _hourly_frame().drop(index=40)
    target = pd.Timestamp("2024-01-03T12:00:00Z")
    assert not build_origins(frame, target_start_utc=target, target_end_utc=target, context_hours=48, horizon_hours=24)


def test_long_frame_and_horizon_selection_choose_only_t_plus_24() -> None:
    frame = _hourly_frame()
    target = pd.Timestamp("2024-01-03T12:00:00Z")
    origins = build_origins(frame, target_start_utc=target, target_end_utc=target, context_hours=24, horizon_hours=24)
    context = contexts_to_long_frame(origins)
    assert len(context) == 24
    forecast = pd.DataFrame(
        {
            "item_id": ["ercot-0", "ercot-0"],
            "timestamp": [origins[0].target_utc - pd.Timedelta(hours=23), origins[0].target_utc],
            "predictions": [1.0, 42.0],
        }
    )
    selected = horizon_predictions(forecast, origins)
    assert selected.loc[0, "chronos2_prediction_mw"] == 42.0
    assert selected.loc[0, "seasonal_naive_prediction_mw"] == origins[0].context_values[-1]


def test_output_requires_exactly_one_target_step() -> None:
    origin = build_origins(
        _hourly_frame(),
        target_start_utc=pd.Timestamp("2024-01-03T12:00:00Z"),
        target_end_utc=pd.Timestamp("2024-01-03T12:00:00Z"),
        context_hours=24,
    )
    with pytest.raises(Chronos2InputError, match="exactly one"):
        horizon_predictions(pd.DataFrame({"item_id": [], "timestamp": [], "predictions": []}), origin)


def test_curve_predictions_preserve_every_hourly_step() -> None:
    origin = build_origins(
        _hourly_frame(),
        target_start_utc=pd.Timestamp("2024-01-03T12:00:00Z"),
        target_end_utc=pd.Timestamp("2024-01-03T12:00:00Z"),
        context_hours=24,
    )
    timestamps = pd.date_range(origin[0].origin_utc + pd.Timedelta(hours=1), origin[0].target_utc, freq="h")
    curve = curve_predictions(
        pd.DataFrame({"item_id": ["ercot-0"] * 24, "timestamp": timestamps, "predictions": range(24)}),
        origin,
    )
    assert curve["horizon_hours"].tolist() == list(range(1, 25))
    assert curve["target_timestamp_utc"].iloc[-1] == origin[0].target_utc
    assert curve["predicted_demand_mw"].iloc[-1] == 23.0


def test_issued_curve_keeps_issuance_separate_from_delayed_context() -> None:
    frame = _hourly_frame(hours=140)
    issuance = pd.Timestamp("2024-01-05T00:00:00Z")
    origins = build_issued_curve_origins(
        frame, forecast_start_utc=issuance, forecast_end_utc=issuance,
        context_hours=24, horizon_hours=24, demand_availability_lag_hours=24,
    )
    assert len(origins) == 1
    assert origins[0].forecast_origin_utc == issuance
    assert origins[0].context_end_utc == issuance - pd.Timedelta(hours=24)
    raw = pd.DataFrame({
        "item_id": ["issued-ercot-0"] * 48,
        "timestamp": pd.date_range(origins[0].context_end_utc + pd.Timedelta(hours=1), periods=48, freq="h"),
        "predictions": range(48),
    })
    curve = issued_curve_predictions(raw, origins)
    assert curve["horizon_hours"].tolist() == list(range(1, 25))
    assert curve.loc[0, "target_timestamp_utc"] == issuance + pd.Timedelta(hours=1)
    assert curve.loc[0, "context_end_utc"] == issuance - pd.Timedelta(hours=24)
    assert curve.loc[0, "baseline_predicted_demand_mw"] == origins[0].context_values[-1]


def test_resume_excludes_only_durably_completed_target_timestamp() -> None:
    frame = _hourly_frame(hours=100)
    origins = build_origins(
        frame,
        target_start_utc=pd.Timestamp("2024-01-03T12:00:00Z"),
        target_end_utc=pd.Timestamp("2024-01-03T14:00:00Z"),
        context_hours=24,
    )
    remaining = pending_origins(origins, {origins[1].target_utc})
    assert [item.target_utc for item in remaining] == [origins[0].target_utc, origins[2].target_utc]
