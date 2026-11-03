"""Focused pure contracts for the AWS-native prospective dashboard evaluator."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path

import pytest

_SOURCE = Path(__file__).resolve().parents[2] / "src" / "texas_energy" / "spark" / "dashboard_evaluation.py"
_SPEC = importlib.util.spec_from_file_location("dashboard_evaluation", _SOURCE)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
_metrics = _MODULE._metrics
_validate_curve = _MODULE._validate_curve


def _curve() -> list[dict[str, object]]:
    origin = datetime(2026, 10, 27, 15, tzinfo=timezone.utc)
    return [
        {
            "horizon_hours": hour,
            "forecast_origin_utc": origin.isoformat().replace("+00:00", "Z"),
            "target_timestamp_utc": (origin + timedelta(hours=hour)).isoformat().replace("+00:00", "Z"),
            "predicted_demand_mw": 50_000.0 + hour,
            "actual_demand_mw": 50_010.0 if hour == 1 else None,
        }
        for hour in range(1, 25)
    ]


def test_prospective_curve_requires_24_finite_hourly_predictions() -> None:
    rows = _validate_curve("20261027T150000Z", "xgboost", _curve())
    assert len(rows) == 24
    assert _metrics(rows) == {
        "matched_observation_count": 1,
        "missing_observation_count": 23,
        "missing_prediction_count": 0,
        "mae_mw": 9.0,
        "rmse_mw": 9.0,
        "bias_mw": -9.0,
    }


def test_prospective_curve_rejects_missing_or_misaligned_prediction() -> None:
    missing = _curve()[:-1]
    with pytest.raises(ValueError, match="exactly horizons"):
        _validate_curve("20261027T150000Z", "xgboost", missing)
    malformed = _curve()
    malformed[4]["target_timestamp_utc"] = malformed[3]["target_timestamp_utc"]
    with pytest.raises(ValueError, match="duplicate"):
        _validate_curve("20261027T150000Z", "xgboost", malformed)


def test_immutable_lineage_matcher_is_relative_to_the_configured_forecast_prefix() -> None:
    match = _MODULE._LINEAGE_PATTERN.match("issue=20261027T150000Z/model=xgboost/run=abc/lineage.json")
    assert match and match.groupdict() == {"issue": "20261027T150000Z", "model": "xgboost", "run": "abc"}


def test_immutable_dashboard_snapshot_rejects_existing_key_without_unsupported_put_header() -> None:
    class _NotFound(Exception):
        response = {"Error": {"Code": "404"}}

    class _Client:
        def __init__(self, exists: bool) -> None:
            self.exists = exists
            self.put_request: dict[str, object] | None = None

        def head_object(self, **_: object) -> None:
            if not self.exists:
                raise _NotFound()

        def put_object(self, **request: object) -> None:
            self.put_request = request

    client = _Client(exists=False)
    _MODULE._put_json("bucket", "immutable.json", {"value": 1}, client, immutable=True)
    assert client.put_request is not None and "IfNoneMatch" not in client.put_request
    with pytest.raises(ValueError, match="already exists"):
        _MODULE._put_json("bucket", "immutable.json", {"value": 1}, _Client(exists=True), immutable=True)
