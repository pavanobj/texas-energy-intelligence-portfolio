"""Regression coverage for final whole-curve chronological split boundaries."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pandas as pd


def _script(name: str):
    path = Path("scripts") / name
    spec = importlib.util.spec_from_file_location(name.replace(".py", ""), path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _rows() -> pd.DataFrame:
    # First curve wholly in train; second crosses train->validation and must
    # be excluded; third is wholly validation; fourth wholly test.
    origins = pd.to_datetime([
        "2024-12-30T00:00:00Z", "2024-12-31T12:00:00Z", "2025-01-02T00:00:00Z", "2025-07-02T00:00:00Z"
    ])
    records = []
    for origin in origins:
        for horizon in range(1, 25):
            records.append({"forecast_origin_utc": origin, "target_timestamp_utc": origin + pd.Timedelta(hours=horizon)})
    return pd.DataFrame(records)


def test_xgboost_final_split_excludes_curves_crossing_boundary() -> None:
    module = _script("train_weather_aware_xgboost_curve.py")
    splits = module._split_chronological_curves(
        _rows(), train_target_end=pd.Timestamp("2024-12-31T23:00:00Z"), validation_target_end=pd.Timestamp("2025-06-30T23:00:00Z")
    )
    assert splits["train"]["forecast_origin_utc"].nunique() == 1
    assert splits["validation"]["forecast_origin_utc"].nunique() == 1
    assert splits["test"]["forecast_origin_utc"].nunique() == 1


def test_bilstm_final_split_uses_the_same_whole_curve_policy() -> None:
    module = _script("train_weather_aware_bilstm_curve.py")
    origins = pd.DatetimeIndex(_rows()["forecast_origin_utc"].drop_duplicates())
    masks = module._split_masks(
        origins, mode="chronological", train_target_end=pd.Timestamp("2024-12-31T23:00:00Z"), validation_target_end=pd.Timestamp("2025-06-30T23:00:00Z")
    )
    assert masks["train"].sum() == masks["validation"].sum() == masks["test"].sum() == 1
