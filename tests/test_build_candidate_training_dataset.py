from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name, Path(__file__).parents[1] / "scripts" / f"{name}.py")
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


_CANDIDATE = _script("build_candidate_training_dataset")
_BILSTM = _script("train_weather_aware_bilstm_curve")
_XGBOOST = _script("train_weather_aware_xgboost_curve")
CandidateDatasetError = _CANDIDATE.CandidateDatasetError
build_dataset = _CANDIDATE.build_dataset
build_full_history_dataset = _CANDIDATE.build_full_history_dataset
_read_bilstm_gold = _BILSTM._gold
_read_gold = _XGBOOST._read_gold


def _sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _bundle(root: Path, issue: pd.Timestamp, gold: pd.DataFrame, *, available_at: pd.Timestamp | None = None) -> None:
    path = root / f"issue={issue.strftime('%Y%m%dT%H%M%SZ')}"; path.mkdir(parents=True)
    context_end = issue - pd.Timedelta(hours=24)
    demand = gold.loc[gold.period_utc.between(context_end - pd.Timedelta(hours=2047), context_end), ["period_utc", "observed_average_demand_mw"]]
    demand.to_parquet(path / "demand_history.parquet", index=False)
    targets = pd.date_range(issue + pd.Timedelta(hours=1), periods=24, freq="h", tz="UTC")
    weather = pd.DataFrame([{"city": city, "valid_time_utc": target, "weather_available_at_utc": available_at or issue, "temperature_2m_c": 20.0, "relative_humidity_pct": 50.0, "apparent_temperature_c": 20.0, "wind_speed_10m_kmh": 10.0} for city in ("austin", "dallas", "el_paso", "houston", "san_antonio") for target in targets])
    weather.to_parquet(path / "weather_forecasts.parquet", index=False)
    manifest = {"forecast_origin_utc": issue.isoformat(), "context_end_utc": context_end.isoformat(), "demand_availability_lag_hours": 24, "demand_sha256": _sha(path / "demand_history.parquet"), "weather_sha256": _sha(path / "weather_forecasts.parquet")}
    (path / "input_manifest.json").write_text(json.dumps(manifest), encoding="utf-8")


def _gold(root: Path) -> pd.DataFrame:
    index = pd.date_range("2026-07-17T23:00:00Z", periods=2500, freq="h")
    gold = pd.DataFrame({"period_utc": index, "observed_average_demand_mw": range(len(index)), "respondent": "ERCO", "demand_unit": "megawatthours", "demand_lag_1h_mwh": 0.0, "demand_lag_24h_mwh": 0.0, "demand_lag_168h_mwh": 0.0, "demand_trailing_mean_24h_mwh": 0.0, "demand_trailing_mean_168h_mwh": 0.0})
    (root / "_delta_log").mkdir(parents=True)
    file = "part-000.parquet"; gold.to_parquet(root / file, index=False)
    (root / "_delta_log" / "00000000000000000000.json").write_text(json.dumps({"add": {"path": file}}) + "\n", encoding="utf-8")
    return gold


def test_candidate_dataset_is_checksum_verified_and_smoke_only(tmp_path: Path) -> None:
    gold_root = tmp_path / "gold"; gold = _gold(gold_root); replay = tmp_path / "replay"
    for day in range(12, 26): _bundle(replay, pd.Timestamp(f"2026-10-{day:02d}T06:00:00Z"), gold)
    dataset = build_dataset(replay_root=replay, gold_demand_delta_root=gold_root, output_root=tmp_path / "datasets")
    manifest = json.loads((dataset / "manifest" / "dataset_manifest.json").read_text())
    assert manifest["promotion_status"] == "smoke_only_not_production_eligible"
    assert len(manifest["issues"]) == 14
    assert manifest["issues"][0]["genuine_gold_target_hours"] == 24
    assert manifest["chronological_split_contract"]["whole_curve_counts"] == {"train": 9, "validation": 2, "test": 3}
    assert (dataset / "gold" / "gold.parquet").is_file()


def test_candidate_dataset_rejects_weather_that_arrived_after_issue(tmp_path: Path) -> None:
    gold_root = tmp_path / "gold"; gold = _gold(gold_root); replay = tmp_path / "replay"
    for day in range(12, 26):
        issue = pd.Timestamp(f"2026-10-{day:02d}T06:00:00Z")
        _bundle(replay, issue, gold, available_at=issue + pd.Timedelta(hours=1) if day == 12 else None)
    with pytest.raises(CandidateDatasetError, match="weather is unavailable"):
        build_dataset(replay_root=replay, gold_demand_delta_root=gold_root, output_root=tmp_path / "datasets")


def test_existing_training_adapters_accept_immutable_candidate_gold_parquet(tmp_path: Path) -> None:
    gold_root = tmp_path / "gold"; original = _gold(gold_root)
    # Historical Parquet can carry the precomputed lag fields as strings.
    # Candidate ingestion must normalize them before XGBoost sees the schema.
    for column in (
        "demand_lag_1h_mwh", "demand_lag_24h_mwh", "demand_lag_168h_mwh",
        "demand_trailing_mean_24h_mwh", "demand_trailing_mean_168h_mwh",
    ):
        original[column] = original[column].map(str)
    path = tmp_path / "gold.parquet"; original.to_parquet(path, index=False)
    loaded = _read_gold(path)
    assert len(loaded) == len(original)
    assert all(pd.api.types.is_float_dtype(loaded[column]) for column in (
        "observed_average_demand_mw", "demand_lag_1h_mwh", "demand_lag_24h_mwh",
        "demand_lag_168h_mwh", "demand_trailing_mean_24h_mwh", "demand_trailing_mean_168h_mwh",
    ))
    assert len(_read_bilstm_gold(path)) == len(original)


def test_full_history_builder_excludes_observed_weather_and_requires_issued_inputs(tmp_path: Path) -> None:
    gold_root = tmp_path / "gold"; gold = _gold(gold_root); weather_root = tmp_path / "weather"
    for day in range(12, 28):
        issue = pd.Timestamp(f"2026-10-{day:02d}T06:00:00Z")
        path = weather_root / f"source={day}"; path.mkdir(parents=True)
        targets = pd.date_range(issue + pd.Timedelta(hours=1), periods=31, freq="h", tz="UTC")
        weather = pd.DataFrame([{"city": city, "valid_time_utc": target, "weather_model_run_utc": issue - pd.Timedelta(hours=6), "weather_available_at_utc": issue, "temperature_2m_c": 20.0, "relative_humidity_pct": 50.0, "apparent_temperature_c": 20.0, "wind_speed_10m_kmh": 10.0} for city in ("austin", "dallas", "el_paso", "houston", "san_antonio") for target in targets])
        weather.to_parquet(path / "weather_forecasts.parquet", index=False)
    dataset = build_full_history_dataset(gold_demand_delta_root=gold_root, issued_weather_roots=(weather_root,), output_root=tmp_path / "datasets")
    manifest = json.loads((dataset / "manifest" / "dataset_manifest.json").read_text())
    assert manifest["quality_assessment"]["causal_eligible_origins"] == 16
    assert manifest["causality"]["observed_weather_excluded_from_features"] is True
    assert manifest["schema_version"] == "candidate-training-v2-full-history"
    assert manifest["chronological_split_contract"]["whole_curve_counts"] == {"train": 9, "validation": 3, "test": 4}
    assert manifest["chronological_split_contract"]["target_overlap_allowed"] is False
    assert len(manifest["source_inventory"]) == 16
    assert [item["issue_id"] for item in manifest["origins"]] == sorted(item["issue_id"] for item in manifest["origins"])
