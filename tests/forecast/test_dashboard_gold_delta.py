"""Focused active-Delta contracts for public dashboard evaluation refreshes."""
from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd


def _publisher():
    script = Path(__file__).resolve().parents[2] / "scripts" / "publish_static_dashboard_data.py"
    spec = importlib.util.spec_from_file_location("dashboard_publisher", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _delta_table(root: Path, rows: list[dict[str, object]], *, stale_rows: list[dict[str, object]] | None = None) -> None:
    (root / "_delta_log").mkdir(parents=True)
    stale = "stale.parquet"
    active = "active.parquet"
    if stale_rows is not None:
        pd.DataFrame(stale_rows).to_parquet(root / stale, index=False)
    pd.DataFrame(rows).to_parquet(root / active, index=False)
    first = [{"add": {"path": stale}}] if stale_rows is not None else []
    (root / "_delta_log" / "00000000000000000000.json").write_text("\n".join(json.dumps(item) for item in first) + ("\n" if first else ""), encoding="utf-8")
    latest = ([{"remove": {"path": stale}}] if stale_rows is not None else []) + [{"add": {"path": active}}]
    (root / "_delta_log" / "00000000000000000001.json").write_text("\n".join(json.dumps(item) for item in latest) + "\n", encoding="utf-8")


def test_gold_demand_uses_only_active_delta_files(tmp_path: Path) -> None:
    publisher = _publisher()
    root = tmp_path / "demand"
    _delta_table(
        root,
        [{"period_utc": "2026-10-26T16:00:00Z", "observed_average_demand_mw": 50000.0, "respondent": "ERCO", "demand_unit": "megawatthours"}],
        stale_rows=[{"period_utc": "2026-10-26T16:00:00Z", "observed_average_demand_mw": 1.0, "respondent": "ERCO", "demand_unit": "megawatthours"}],
    )
    actuals = publisher._gold_actuals(root)
    assert actuals.to_dict("records") == [{"period_utc": "2026-10-26T16:00:00Z", "observed_average_demand_mw": 50000.0, "respondent": "ERCO", "value-units": "megawatthours"}]


def test_gold_weather_only_publishes_matching_target_city_hours(tmp_path: Path) -> None:
    publisher = _publisher()
    root = tmp_path / "weather"
    _delta_table(root, [{"period_utc": "2026-10-26T16:00:00Z", "city": "austin", "source": "open_meteo", "dataset": "historical-weather", "temperature_2m": 22.0, "relative_humidity_2m": 55.0, "wind_speed_10m": 5.0, "temperature_unit": "°C", "relative_humidity_unit": "%", "wind_speed_unit": "km/h", "ingested_at_utc": "2026-10-26T17:00:00Z"}])
    records = publisher._observed_weather(root, ["2026-10-26T16:00:00Z", "2026-10-26T17:00:00Z"])
    assert records == [{"city": "Austin", "target_timestamp_utc": "2026-10-26T16:00:00+00:00", "source": "open_meteo", "dataset": "historical-weather", "observed_at_utc": "2026-10-26T17:00:00+00:00", "temperature_2m_c": 22.0, "relative_humidity_pct": 55.0, "wind_speed_10m_kmh": 5.0, "temperature_unit": "°C", "relative_humidity_unit": "%", "wind_speed_unit": "km/h"}]
