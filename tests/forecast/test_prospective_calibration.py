from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys

import pandas as pd
import pytest

from texas_energy.forecast.calibration import calibrated_curve, load_prospective_calibration
from texas_energy.forecast.prospective import validate_input_bundle

def _runner_script():
    path = Path(__file__).parents[2] / "scripts" / "run_prospective_forecast_trial.py"
    spec = importlib.util.spec_from_file_location("prospective_runner_calibration_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(root: Path) -> Path:
    path = Path(__file__).with_name("test_prospective.py")
    spec = importlib.util.spec_from_file_location("prospective_test_helpers", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module._bundle(root)


def _calibration():
    return load_prospective_calibration(
        Path(__file__).parents[2] / "config" / "prospective_calibration_2025_validation_v1.json"
    )


def test_fixed_validation_coefficients_are_complete_and_output_only() -> None:
    calibration = _calibration()
    assert calibration.calibration_id == "prospective-calibration-2025-validation-v1"
    assert calibration.xgboost_offsets_mw[1] == pytest.approx(1605.2623263888888)
    assert calibration.xgboost_offsets_mw[24] == pytest.approx(1487.1823567708334)
    assert calibration.bilstm_offset_mw == pytest.approx(1589.6361083984375)
    raw = pd.DataFrame(
        {
            "horizon_hours": range(1, 25),
            "target_timestamp_utc": pd.date_range("2026-01-01T01:00:00Z", periods=24, freq="h"),
            "predicted_demand_mw": [50000.0] * 24,
        }
    )
    xgboost = calibrated_curve(raw, base_model="xgboost", calibration=calibration)
    bilstm = calibrated_curve(raw, base_model="bilstm", calibration=calibration)
    assert raw["predicted_demand_mw"].eq(50000.0).all()
    assert xgboost["predicted_demand_mw"].iloc[[0, -1]].tolist() == pytest.approx(
        [51605.26232638889, 51487.18235677083]
    )
    assert bilstm["predicted_demand_mw"].tolist() == pytest.approx(
        [51589.63610839844] * 24
    )


def test_runner_emits_separate_raw_and_calibrated_model_variants(tmp_path: Path) -> None:
    inputs = validate_input_bundle(_bundle(tmp_path / "bundle"))
    raw = pd.DataFrame(
        {
            "horizon_hours": range(1, 25),
            "target_timestamp_utc": inputs.issue_utc
            + pd.to_timedelta(range(1, 25), unit="h"),
            "predicted_demand_mw": [50000.0] * 24,
        }
    )
    variants = _runner_script()._raw_and_calibrated_variants(
        raw, name="xgboost", inputs=inputs, calibration=_calibration()
    )
    output = pd.concat(variants, ignore_index=True)
    assert len(output) == 48
    assert set(output["forecast_variant"]) == {"raw", "calibrated"}
    assert not output.duplicated(
        ["base_model_id", "forecast_variant", "target_timestamp_utc"]
    ).any()
    assert output.loc[output["forecast_variant"].eq("raw"), "calibration_id"].isna().all()
    calibrated = output.loc[output["forecast_variant"].eq("calibrated")]
    assert calibrated["calibration_id"].eq(_calibration().calibration_id).all()
    assert calibrated["predicted_demand_mw"].iloc[0] == pytest.approx(51605.26232638889)


def test_artifact_provenance_uses_hashes_and_not_absolute_host_paths(tmp_path: Path) -> None:
    artifact = tmp_path / "private" / "xgboost"
    artifact.mkdir(parents=True)
    for filename in ("weather_aware.json", "model_contract.json"):
        (artifact / filename).write_text(filename, encoding="utf-8")
    provenance = _runner_script()._artifact_provenance(
        ("xgboost",), {"xgboost": artifact, "bilstm": None, "chronos": None}
    )["xgboost"]
    assert provenance["safe_artifact_label"] == "private/xgboost"
    assert str(tmp_path) not in str(provenance)
    assert set(provenance["files_sha256"]) == {"weather_aware.json", "model_contract.json"}


def test_runner_persists_raw_and_calibrated_curves_with_provenance(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    bundle = _bundle(tmp_path / "bundle")
    inputs = validate_input_bundle(bundle)
    runner = _runner_script()
    artifacts = {}
    for name, files in runner._ARTIFACT_FILES.items():
        artifact = tmp_path / "artifacts" / name
        artifact.mkdir(parents=True)
        for filename in files:
            (artifact / filename).write_text(filename, encoding="utf-8")
        artifacts[name] = artifact

    def fake_curve(*_args, **_kwargs):
        return pd.DataFrame(
            {
                "horizon_hours": range(1, 25),
                "target_timestamp_utc": inputs.issue_utc
                + pd.to_timedelta(range(1, 25), unit="h"),
                "predicted_demand_mw": [50000.0] * 24,
            }
        )

    monkeypatch.setattr(runner, "_xgboost", fake_curve)
    monkeypatch.setattr(runner, "_bilstm", fake_curve)
    monkeypatch.setattr(runner, "_chronos", fake_curve)
    output_root = tmp_path / "outputs"
    calibration_path = Path(__file__).parents[2] / "config" / "prospective_calibration_2025_validation_v1.json"
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "run_prospective_forecast_trial.py",
            "--input-bundle",
            str(bundle),
            "--output-root",
            str(output_root),
            "--models",
            "xgboost",
            "bilstm",
            "chronos",
            "--xgboost-artifact",
            str(artifacts["xgboost"]),
            "--bilstm-artifact",
            str(artifacts["bilstm"]),
            "--chronos-artifact",
            str(artifacts["chronos"]),
            "--calibration-config",
            str(calibration_path),
            "--configuration-sha256",
            "f" * 64,
        ],
    )
    assert runner.main() == 0
    issue = output_root / "issue=20260102T060000Z"
    predictions = pd.read_parquet(issue / "forecast_predictions.parquet")
    manifest = json.loads((issue / "issue_manifest.json").read_text(encoding="utf-8"))
    assert len(predictions) == 120
    assert predictions.groupby(["base_model_id", "forecast_variant"]).size().to_dict() == {
        ("amazon/chronos-2", "raw"): 24,
        ("weather_aware_bilstm_curve", "calibrated"): 24,
        ("weather_aware_bilstm_curve", "raw"): 24,
        ("weather_aware_xgboost_curve", "calibrated"): 24,
        ("weather_aware_xgboost_curve", "raw"): 24,
    }
    assert manifest["schema_version"] == "1.1"
    assert manifest["calibration"]["calibration_id"] == _calibration().calibration_id
    assert manifest["configuration_sha256"] == "f" * 64
    assert set(manifest["model_artifacts"]) == {"xgboost", "bilstm", "chronos"}
