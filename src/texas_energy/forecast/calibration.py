"""Versioned, output-only calibration for prospective forecast curves."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

import pandas as pd

from .prospective import ProspectiveTrialError, sha256_file


@dataclass(frozen=True)
class ProspectiveCalibration:
    """Immutable calibrated-output contract; it never changes model weights."""

    calibration_id: str
    sha256: str
    xgboost_offsets_mw: dict[int, float]
    bilstm_offset_mw: float


def load_prospective_calibration(path: Path) -> ProspectiveCalibration:
    """Load a strict, reviewed calibration definition without refitting it."""

    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        xgboost = payload["xgboost"]
        bilstm = payload["bilstm"]
        offsets = {int(horizon): float(value) for horizon, value in xgboost["offsets_mw"].items()}
        calibration_id = str(payload["calibration_id"])
        bilstm_offset = float(bilstm["offset_mw"])
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        raise ProspectiveTrialError("Prospective calibration configuration is invalid.") from None
    if (
        payload.get("schema_version") != "1.0"
        or xgboost.get("kind") != "horizon_additive_mw"
        or bilstm.get("kind") != "global_additive_mw"
        or not calibration_id
        or set(offsets) != set(range(1, 25))
        or not all(math.isfinite(value) for value in offsets.values())
        or not math.isfinite(bilstm_offset)
    ):
        raise ProspectiveTrialError("Prospective calibration contract is incomplete or non-finite.")
    return ProspectiveCalibration(
        calibration_id=calibration_id,
        sha256=sha256_file(path),
        xgboost_offsets_mw=offsets,
        bilstm_offset_mw=bilstm_offset,
    )


def calibrated_curve(
    raw: pd.DataFrame,
    *,
    base_model: str,
    calibration: ProspectiveCalibration,
) -> pd.DataFrame:
    """Return a companion curve and leave the original raw values untouched."""

    if set(raw["horizon_hours"]) != set(range(1, 25)) or len(raw) != 24:
        raise ProspectiveTrialError("Calibration requires one complete 24-hour raw curve.")
    result = raw.copy()
    if base_model == "xgboost":
        offsets = result["horizon_hours"].map(calibration.xgboost_offsets_mw)
        if offsets.isna().any():
            raise ProspectiveTrialError("XGBoost calibration offsets do not cover every horizon.")
        result["predicted_demand_mw"] = result["predicted_demand_mw"] + offsets
    elif base_model == "bilstm":
        result["predicted_demand_mw"] = result["predicted_demand_mw"] + calibration.bilstm_offset_mw
    else:
        raise ProspectiveTrialError("Only XGBoost and Bi-LSTM have approved calibration.")
    return result
