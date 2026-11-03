import json
import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "prepare_sagemaker_validation_assets",
    Path(__file__).parents[1] / "scripts" / "prepare_sagemaker_validation_assets.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
AssetPreparationError = _MODULE.AssetPreparationError
prepare_assets = _MODULE.prepare_assets


def _write(path: Path, value: bytes = b"artifact") -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(value)


def test_prepares_exact_xgboost_bilstm_and_input_files(tmp_path: Path) -> None:
    xgb, bilstm, bundle, stage = (tmp_path / name for name in ("xgb", "bilstm", "bundle", "stage"))
    _write(xgb / "weather_aware.json")
    _write(xgb / "model_contract.json", b"{}")
    _write(xgb / "metrics.json", b"{}")
    _write(bilstm / "weather_aware_best.pt")
    _write(bilstm / "preprocessing.json", b"{}")
    _write(bilstm / "model_contract.json", b"{}")
    _write(bilstm / "metrics.json", b"{}")
    _write(bundle / "demand_history.parquet")
    _write(bundle / "weather_forecasts.parquet")
    _write(bundle / "input_manifest.json", json.dumps({"forecast_origin_utc": "2025-12-30T06:00:00+00:00"}).encode())
    manifest = prepare_assets(
        xgboost_dir=xgb, bilstm_dir=bilstm, input_bundle=bundle, stage_dir=stage,
        loader_revision="abc123", model_version="final-v1", chronos_dir=None,
    )
    result = json.loads(manifest.read_text())
    assert result["incomplete"] is True
    assert (stage / "models/v1/xgboost/final-v1/weather_aware.json").is_file()
    assert (stage / "models/v1/bilstm/final-v1/weather_aware_best.pt").is_file()
    assert (stage / "forecast-inputs/v1/historical-replay/issue=20251230T060000Z/demand_history.parquet").is_file()


def test_refuses_nonempty_immutable_stage_directory(tmp_path: Path) -> None:
    stage = tmp_path / "stage"; stage.mkdir(); _write(stage / "old")
    with pytest.raises(AssetPreparationError, match="must not already contain"):
        prepare_assets(
            xgboost_dir=tmp_path, bilstm_dir=tmp_path, input_bundle=tmp_path,
            stage_dir=stage, loader_revision="abc123", model_version="final-v1",
        )
