"""Focused contract tests for the static-dashboard publisher."""
from __future__ import annotations

import importlib.util
import json
from decimal import Decimal
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "publish_static_dashboard_data.py"
_SPEC = importlib.util.spec_from_file_location("static_publisher", _SCRIPT)
assert _SPEC and _SPEC.loader
publisher = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(publisher)

def test_public_payload_rejects_secrets_and_absolute_paths() -> None:
    with pytest.raises(publisher.PublishError, match="unsafe"):
        publisher.public({"source": "<LOCAL_PATH>/private.parquet"})
    with pytest.raises(publisher.PublishError, match="unsafe"):
        publisher.public({"api_key": "not-a-real-secret"})

def test_finite_rejects_nan_and_infinity() -> None:
    assert publisher.finite(42) == 42.0
    assert publisher.finite(Decimal("42.0")) == 42.0
    with pytest.raises(publisher.PublishError, match="Non-finite"):
        publisher.finite(float("nan"))
    with pytest.raises(publisher.PublishError, match="Non-finite"):
        publisher.finite(float("inf"))


def test_finite_or_pending_keeps_unavailable_gold_values_pending() -> None:
    assert publisher.finite_or_pending(None) is None
    assert publisher.finite_or_pending(float("nan")) is None
    assert publisher.finite_or_pending(42) == 42.0

def test_atomic_write_replaces_complete_json(tmp_path: Path) -> None:
    target = tmp_path / "published.json"
    publisher.atomic(target, {"schema_version": "1.0", "records": [1, 2]})
    assert json.loads(target.read_text(encoding="utf-8")) == {"schema_version": "1.0", "records": [1, 2]}
    assert not target.with_suffix(".tmp").exists()


def test_latest_forecast_schema_rejects_wrong_horizon_timestamp() -> None:
    document = {
        "schema_version": "1.0",
        "status": "historical_replay",
        "historical_not_live": True,
        "unit": "MW",
        "forecast_origin_utc": "2025-01-01T00:00:00+00:00",
        "predictions": [
            {
                "horizon_hours": hour,
                "target_timestamp_utc": f"2025-01-{hour:02d}T00:00:00+00:00",
                "actual_demand_mw": 1.0,
                "xgboost_weather_aware_mw": 1.0,
                "bilstm_weather_aware_mw": 1.0,
                "chronos2_mw": 1.0,
            }
            for hour in range(1, 25)
        ],
    }
    with pytest.raises(publisher.PublishError, match="continuity"):
        publisher.validate_document("latest-forecast.json", document)
