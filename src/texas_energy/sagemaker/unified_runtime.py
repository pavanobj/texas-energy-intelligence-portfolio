"""Genuine artifact-backed SageMaker 24-hour forecasting dispatcher.

This is an inference/replay runtime.  It deliberately accepts the immutable
prospective input-bundle contract rather than treating a Gold snapshot with
future observed weather as a prospective model input.
"""

from __future__ import annotations

import argparse
import json
import math
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from texas_energy.forecast.bilstm_curve import WEATHER_COLUMNS, build_direct_curve
from texas_energy.forecast.chronos2 import (
    Chronos2IssuedCurveOrigin,
    issued_curve_contexts_to_long_frame,
    issued_curve_predictions,
)
from texas_energy.forecast.curve_contract import validate_forecast_curve
from texas_energy.forecast.prospective import (
    CITIES,
    ProspectiveInputs,
    ProspectiveTrialError,
    sha256_file,
    validate_input_bundle,
    xgboost_feature_frame,
)
from texas_energy.forecast.xgboost_curve import weather_aware_feature_columns
from texas_energy.mlops.model_lifecycle import ModelLifecycleError, load_json as load_lifecycle_json, resolve_approved_artifact


HORIZON_HOURS = 24
XGBOOST_CONTEXT_HOURS = 168
CHRONOS_CONTEXT_HOURS = 2048
MODELS = ("xgboost", "bilstm", "chronos2")
MODEL_IDS = {
    "xgboost": "weather_aware_xgboost_curve",
    "bilstm": "weather_aware_bilstm_curve",
    "chronos2": "amazon/chronos-2",
}
FEATURE_CONTRACTS = {
    "xgboost": "direct_weather_aware_xgboost_curve-v1",
    "bilstm": "direct_weather_aware_bilstm_curve-v1",
    "chronos2": "chronos2_demand_context_2048-v1",
}


class SageMakerRuntimeError(RuntimeError):
    """A job cannot prove a complete, causal model invocation."""


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument(
        "--input-bundle", type=Path,
        default=Path(os.environ.get("SM_CHANNEL_INPUT", "/opt/ml/input/data/input")),
    )
    parser.add_argument(
        "--artifact-dir", type=Path,
        default=Path(os.environ.get("SM_CHANNEL_MODEL", "/opt/ml/input/data/model")),
    )
    parser.add_argument(
        "--output-dir", type=Path,
        default=Path(os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data")),
    )
    parser.add_argument("--run-id", default=os.environ.get("SM_TRAINING_JOB_NAME", "local"))
    parser.add_argument(
        "--generation-type", choices=("historical_replay", "prospective_inference"),
        default="historical_replay",
    )
    parser.add_argument("--chronos-device", default="cpu")
    parser.add_argument(
        "--approved-model-selection", type=Path,
        help="Optional reviewed approved-model-selection JSON. Omitted for legacy static artifact channels.",
    )
    parser.add_argument("--validate-only", action="store_true")
    return parser.parse_args()


def _artifact_files(model: str, artifact: Path) -> dict[str, Path]:
    names = {
        "xgboost": ("weather_aware.json", "model_contract.json"),
        "bilstm": ("weather_aware_best.pt", "preprocessing.json", "model_contract.json"),
        "chronos2": ("config.json", "model.safetensors"),
    }[model]
    files = {name: artifact / name for name in names}
    if not artifact.is_dir() or not all(path.is_file() for path in files.values()):
        raise SageMakerRuntimeError(f"{model} requires a complete immutable artifact directory.")
    return files


def _json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise SageMakerRuntimeError(f"Artifact metadata is unreadable: {path.name}") from error
    if not isinstance(value, dict):
        raise SageMakerRuntimeError(f"Artifact metadata must be an object: {path.name}")
    return value


def artifact_provenance(model: str, artifact: Path) -> dict[str, Any]:
    files = _artifact_files(model, artifact)
    contract: dict[str, Any] | None = None
    if "model_contract.json" in files:
        contract = _json(files["model_contract.json"])
        if contract.get("horizons") != list(range(1, 25)):
            raise SageMakerRuntimeError(f"{model} contract does not emit horizons 1 through 24.")
        if contract.get("demand_availability_lag_hours") != 24:
            raise SageMakerRuntimeError(f"{model} contract does not preserve the 24-hour demand lag.")
        if model == "bilstm" and contract.get("context_hours") != XGBOOST_CONTEXT_HOURS:
            raise SageMakerRuntimeError("Bi-LSTM contract does not use the 168-hour demand context.")
        if model == "xgboost" and contract.get("weather_aware_features") != list(weather_aware_feature_columns(CITIES)):
            raise SageMakerRuntimeError("XGBoost weather-aware feature contract differs from the accepted artifact.")
    return {
        "model_id": MODEL_IDS[model],
        "feature_contract_version": FEATURE_CONTRACTS[model],
        "safe_artifact_label": artifact.name or "model-artifact",
        "files_sha256": {name: sha256_file(path) for name, path in files.items()},
        "model_contract": contract,
    }


def resolve_artifact_channel(args: argparse.Namespace) -> tuple[Path, dict[str, Any] | None]:
    """Resolve an approval-gated artifact, never an arbitrary latest candidate.

    Current deployed jobs omit ``--approved-model-selection`` and retain their
    frozen legacy artifact channels. A future reviewed state-machine revision
    will supply the immutable selection document and its approved-artifacts
    root together only after an initial champion has been seeded.
    """

    if args.approved_model_selection is None:
        return args.artifact_dir, None
    if args.model == "chronos2":
        raise SageMakerRuntimeError("Chronos-2 is independent of the trainable approved-model selector.")
    try:
        artifact, entry = resolve_approved_artifact(
            selection=load_lifecycle_json(args.approved_model_selection),
            model=args.model, artifacts_root=args.artifact_dir,
        )
    except ModelLifecycleError as error:
        raise SageMakerRuntimeError("Approved model selection is invalid; refusing candidate fallback.") from error
    expected = entry["required_files_sha256"]
    actual = _artifact_files(args.model, artifact)
    if not set(expected).issubset(actual) or any(sha256_file(actual[name]) != digest for name, digest in expected.items()):
        raise SageMakerRuntimeError("Approved artifact checksum differs from the selected immutable version.")
    return artifact, entry


def _xgboost_columns(model: object, features: pd.DataFrame) -> list[str]:
    columns = model.get_booster().feature_names
    if not columns or set(columns).difference(features.columns):
        raise SageMakerRuntimeError("XGBoost feature order differs from the accepted artifact contract.")
    return list(columns)


def xgboost_curve(inputs: ProspectiveInputs, artifact: Path) -> pd.DataFrame:
    """Load the final weather-aware direct 24-step XGBoost artifact."""

    from xgboost import XGBRegressor

    features, _ = xgboost_feature_frame(inputs)
    model = XGBRegressor()
    model.load_model(str(_artifact_files("xgboost", artifact)["weather_aware.json"]))
    values = model.predict(features.loc[:, _xgboost_columns(model, features)])
    return pd.DataFrame({
        "horizon_hours": range(1, 25),
        "target_timestamp_utc": features["target_timestamp_utc"],
        "predicted_demand_mw": values,
    })


def _bilstm_scaled_inputs(inputs: ProspectiveInputs, artifact: Path) -> tuple[np.ndarray, np.ndarray, dict[str, Any]]:
    prep = _json(_artifact_files("bilstm", artifact)["preprocessing.json"])
    required = ("demand_mean", "demand_std", "target_mean", "target_std", "weather_mean", "weather_std", "weather_columns")
    if any(key not in prep for key in required) or prep["weather_columns"] != list(WEATHER_COLUMNS):
        raise SageMakerRuntimeError("Bi-LSTM preprocessing does not match the five-city weather contract.")
    history = pd.date_range(inputs.context_end_utc - pd.Timedelta(hours=167), inputs.context_end_utc, freq="h", tz="UTC")
    demand = inputs.demand.set_index("period_utc")["observed_average_demand_mw"].reindex(history)
    features, _ = xgboost_feature_frame(inputs)
    weather = features.loc[:, list(WEATHER_COLUMNS)].to_numpy(dtype=np.float32)
    mean = np.asarray(prep["weather_mean"], dtype=np.float32)
    std = np.asarray(prep["weather_std"], dtype=np.float32)
    demand_std = float(prep["demand_std"])
    if demand.isna().any() or weather.shape != (24, len(WEATHER_COLUMNS)) or mean.shape != std.shape or len(mean) != len(WEATHER_COLUMNS) or not math.isfinite(demand_std) or demand_std == 0:
        raise SageMakerRuntimeError("Bi-LSTM input dimensions or train-only scalers are invalid.")
    return (
        ((demand.to_numpy(dtype=np.float32) - float(prep["demand_mean"])) / demand_std).astype(np.float32),
        ((weather - mean) / np.where(std == 0, 1, std)).astype(np.float32),
        prep,
    )


def bilstm_curve(inputs: ProspectiveInputs, artifact: Path) -> pd.DataFrame:
    """Load the genuine float32, 168-hour, weather-aware Bi-LSTM checkpoint."""

    import torch

    demand, weather, prep = _bilstm_scaled_inputs(inputs, artifact)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    checkpoint = torch.load(_artifact_files("bilstm", artifact)["weather_aware_best.pt"], map_location=device, weights_only=True)
    if not isinstance(checkpoint, dict) or "state_dict" not in checkpoint:
        raise SageMakerRuntimeError("Bi-LSTM checkpoint lacks a state_dict.")
    model = build_direct_curve(torch, weather.shape[-1], True).to(device)
    model.load_state_dict(checkpoint["state_dict"])
    model.eval()
    with torch.inference_mode():
        prediction = model(
            torch.from_numpy(demand[None, :, None]).to(device, dtype=torch.float32),
            torch.from_numpy(weather[None, :, :]).to(device, dtype=torch.float32),
        ).detach().cpu().numpy()[0]
    features, _ = xgboost_feature_frame(inputs)
    return pd.DataFrame({
        "horizon_hours": range(1, 25),
        "target_timestamp_utc": features["target_timestamp_utc"],
        "predicted_demand_mw": prediction * float(prep["target_std"]) + float(prep["target_mean"]),
    })


def chronos2_curve(inputs: ProspectiveInputs, artifact: Path, *, device: str) -> pd.DataFrame:
    """Run pretrained Chronos-2 zero-shot inference using delayed demand only."""

    from chronos import Chronos2Pipeline

    _artifact_files("chronos2", artifact)
    index = pd.date_range(inputs.context_end_utc - pd.Timedelta(hours=2047), inputs.context_end_utc, freq="h", tz="UTC")
    history = inputs.demand.set_index("period_utc")["observed_average_demand_mw"].reindex(index)
    if history.isna().any():
        raise SageMakerRuntimeError("Chronos-2 requires a complete 2,048-hour delayed-demand context.")
    origin = Chronos2IssuedCurveOrigin(inputs.issue_utc, inputs.context_end_utc, history.to_numpy(dtype=np.float64), np.repeat(np.nan, 24), 24)
    pipeline = Chronos2Pipeline.from_pretrained(str(artifact), device_map=device)
    raw = pipeline.predict_df(
        issued_curve_contexts_to_long_frame((origin,)), prediction_length=48,
        # Preserve the established prospective Chronos configuration.  It
        # exposes a central estimate only; no synthetic interval is emitted.
        quantile_levels=[0.5], batch_size=1, context_length=2048, freq="h",
    )
    result = issued_curve_predictions(raw, (origin,)).loc[:, ["horizon_hours", "target_timestamp_utc", "predicted_demand_mw"]]
    result["prediction_lower_mw"] = pd.NA
    result["prediction_upper_mw"] = pd.NA
    return result


def _complete_curve(model: str, raw: pd.DataFrame, inputs: ProspectiveInputs, run_id: str) -> pd.DataFrame:
    result = raw.copy()
    result["model_id"] = MODEL_IDS[model]
    result["run_id"] = run_id
    result["forecast_origin_utc"] = inputs.issue_utc
    result["feature_as_of_utc"] = inputs.context_end_utc
    result["forecast_variant"] = "raw"
    result["calibration_id"] = pd.NA
    result["demand_availability_lag_hours"] = 24
    result["forecast_generation_type"] = "sagemaker_batch"
    checked = validate_forecast_curve(result)
    if len(checked) != HORIZON_HOURS:
        raise SageMakerRuntimeError("A job must emit exactly one T+1 through T+24 curve.")
    return checked


def _write_json(path: Path, value: dict[str, Any]) -> None:
    path.write_text(json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n", encoding="utf-8")


def execute(args: argparse.Namespace) -> dict[str, Any]:
    try:
        inputs = validate_input_bundle(args.input_bundle, context_hours=CHRONOS_CONTEXT_HOURS)
    except ProspectiveTrialError as error:
        raise SageMakerRuntimeError("Input bundle fails the causal prospective contract.") from error
    artifact_dir, selection_entry = resolve_artifact_channel(args)
    artifact = artifact_provenance(args.model, artifact_dir)
    lineage = {
        "schema_version": "sagemaker-unified-forecast-v2",
        "model": args.model, "model_id": MODEL_IDS[args.model], "run_id": args.run_id,
        "forecast_origin_utc": inputs.issue_utc.isoformat(), "context_end_utc": inputs.context_end_utc.isoformat(),
        "demand_availability_lag_hours": 24, "horizon_hours": 24, "demand_unit": "MW",
        "generation_type": args.generation_type,
        "input_bundle_manifest_sha256": sha256_file(args.input_bundle / "input_manifest.json"),
        "artifact": artifact,
        "approved_model_selection": selection_entry,
    }
    if args.validate_only:
        return {"status": "validated", **lineage}
    raw = {
        "xgboost": lambda: xgboost_curve(inputs, artifact_dir),
        "bilstm": lambda: bilstm_curve(inputs, artifact_dir),
        "chronos2": lambda: chronos2_curve(inputs, artifact_dir, device=args.chronos_device),
    }[args.model]()
    forecasts = _complete_curve(args.model, raw, inputs, args.run_id)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    prediction_path = args.output_dir / "predictions.parquet"
    forecasts.to_parquet(prediction_path, index=False)
    lineage.update({
        "prediction_sha256": sha256_file(prediction_path),
        "weather_input": "issued_forecast_available_at_or_before_origin",
        "uncertainty": "chronos_quantiles_when_exposed_by_runtime" if args.model == "chronos2" else "not_available_for_point_estimate_artifact",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
    })
    _write_json(args.output_dir / "lineage.json", lineage)
    _write_json(args.output_dir / "metrics.json", {
        "schema_version": "sagemaker-unified-metrics-v1", "status": "not_scored_without_post_target_actuals",
        "model_id": MODEL_IDS[args.model], "run_id": args.run_id, "forecast_rows": len(forecasts),
    })
    return {"status": "completed", "rows": len(forecasts), **lineage}


def main() -> int:
    print(json.dumps(execute(arguments()), sort_keys=True))
    return 0
