from pathlib import Path
from types import SimpleNamespace

import pandas as pd
import pytest

from texas_energy.forecast.prospective import ProspectiveInputs
from texas_energy.forecast.xgboost_curve import weather_aware_feature_columns
from texas_energy.sagemaker.unified_runtime import (
    HORIZON_HOURS,
    SageMakerRuntimeError,
    _complete_curve,
    artifact_provenance,
    resolve_artifact_channel,
)


def _inputs() -> ProspectiveInputs:
    origin = pd.Timestamp("2025-12-30T06:00:00Z")
    return ProspectiveInputs(
        issue_utc=origin,
        context_end_utc=origin - pd.Timedelta(hours=24),
        demand=pd.DataFrame(),
        weather=pd.DataFrame(),
        manifest={},
        bundle_path=Path("bundle"),
    )


def test_shared_output_contract_is_a_complete_24_hour_curve() -> None:
    origin = _inputs().issue_utc
    raw = pd.DataFrame(
        {
            "horizon_hours": range(1, 25),
            "target_timestamp_utc": origin + pd.to_timedelta(range(1, 25), unit="h"),
            "predicted_demand_mw": [50_000.0] * HORIZON_HOURS,
        }
    )
    result = _complete_curve("xgboost", raw, _inputs(), "test")
    assert len(result) == HORIZON_HOURS
    assert result["target_timestamp_utc"].iloc[-1] == origin + pd.Timedelta(hours=24)
    assert result["forecast_variant"].eq("raw").all()


def test_output_contract_rejects_misaligned_target_timestamp() -> None:
    raw = pd.DataFrame(
        {
            "horizon_hours": range(1, 25),
            "target_timestamp_utc": [_inputs().issue_utc] * HORIZON_HOURS,
            "predicted_demand_mw": [50_000.0] * HORIZON_HOURS,
        }
    )
    with pytest.raises(ValueError, match="target_timestamp_utc"):
        _complete_curve("xgboost", raw, _inputs(), "test")


def test_xgboost_artifact_requires_exact_weather_feature_contract(tmp_path: Path) -> None:
    artifact = tmp_path / "xgboost"
    artifact.mkdir()
    (artifact / "weather_aware.json").write_bytes(b"not-loaded-in-contract-test")
    (artifact / "model_contract.json").write_text(
        '{"horizons": [1,2,3,4,5,6,7,8,9,10,11,12,13,14,15,16,17,18,19,20,21,22,23,24], '
        '"demand_availability_lag_hours": 24, "weather_aware_features": []}',
        encoding="utf-8",
    )
    with pytest.raises(SageMakerRuntimeError, match="feature contract"):
        artifact_provenance("xgboost", artifact)


def test_xgboost_contract_accepts_stable_feature_order(tmp_path: Path) -> None:
    artifact = tmp_path / "xgboost"
    artifact.mkdir()
    (artifact / "weather_aware.json").write_bytes(b"contract-only")
    columns = list(weather_aware_feature_columns(("austin", "dallas", "el_paso", "houston", "san_antonio")))
    (artifact / "model_contract.json").write_text(
        __import__("json").dumps(
            {
                "horizons": list(range(1, 25)),
                "demand_availability_lag_hours": 24,
                "weather_aware_features": columns,
            }
        ),
        encoding="utf-8",
    )
    assert artifact_provenance("xgboost", artifact)["feature_contract_version"].endswith("v1")


def test_approved_selection_resolves_only_the_selected_checksums(tmp_path: Path) -> None:
    root = tmp_path / "approved"; artifact = root / "xgboost" / "version=reviewed"; artifact.mkdir(parents=True)
    model = artifact / "weather_aware.json"; model.write_bytes(b"reviewed-model")
    contract = artifact / "model_contract.json"
    columns = list(weather_aware_feature_columns(("austin", "dallas", "el_paso", "houston", "san_antonio")))
    contract.write_text(__import__("json").dumps({"horizons": list(range(1, 25)), "demand_availability_lag_hours": 24, "weather_aware_features": columns}), encoding="utf-8")
    import hashlib
    selection = {
        "schema_version": "texas-energy-approved-model-selection-v1", "status": "approved",
        "models": {
            "xgboost": {"model": "xgboost", "version_id": "reviewed", "artifact_relative_path": "xgboost/version=reviewed", "required_files_sha256": {"weather_aware.json": hashlib.sha256(model.read_bytes()).hexdigest(), "model_contract.json": hashlib.sha256(contract.read_bytes()).hexdigest()}},
            "bilstm": {"model": "bilstm", "version_id": "legacy", "artifact_relative_path": "bilstm/version=legacy", "required_files_sha256": {"weather_aware_best.pt": "a" * 64}},
        },
    }
    selection_path = tmp_path / "selection.json"; selection_path.write_text(__import__("json").dumps(selection), encoding="utf-8")
    resolved, entry = resolve_artifact_channel(SimpleNamespace(model="xgboost", artifact_dir=root, approved_model_selection=selection_path))
    assert resolved == artifact.resolve()
    assert entry and entry["version_id"] == "reviewed"


def test_disabled_selection_leaves_legacy_artifact_channel_unchanged(tmp_path: Path) -> None:
    artifact, entry = resolve_artifact_channel(SimpleNamespace(model="xgboost", artifact_dir=tmp_path, approved_model_selection=None))
    assert artifact == tmp_path
    assert entry is None


def test_missing_approved_selection_fails_closed(tmp_path: Path) -> None:
    with pytest.raises(SageMakerRuntimeError, match="refusing candidate fallback"):
        resolve_artifact_channel(SimpleNamespace(model="xgboost", artifact_dir=tmp_path, approved_model_selection=tmp_path / "absent.json"))
