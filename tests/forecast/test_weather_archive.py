from __future__ import annotations

import json

import pandas as pd

from texas_energy.forecast.weather_archive import audit_archive


def _run(root, day: str, *, complete: bool = True) -> None:
    run = root / f"run={day}T00Z"; run.mkdir()
    periods = pd.date_range(f"{day}T00:00:00Z", periods=2 if complete else 1, freq="h")
    rows = [{"city": city, "valid_time_utc": period, "weather_model_run_utc": f"{day}T00:00:00Z", "weather_available_at_utc": f"{day}T06:00:00Z", "temperature_2m_c": 20.0, "relative_humidity_pct": 50.0, "apparent_temperature_c": 19.0, "wind_speed_10m_kmh": 10.0} for city in ("austin", "dallas") for period in periods]
    pd.DataFrame(rows).to_parquet(run / "weather_forecasts.parquet", index=False)
    (run / "manifest.json").write_text(json.dumps({}), encoding="utf-8")


def test_archive_audit_requires_each_run_and_balanced_city_hours(tmp_path) -> None:
    _run(tmp_path, "2024-03-14")
    report = audit_archive(roots=(tmp_path,), start=pd.Timestamp("2024-03-14").date(), end=pd.Timestamp("2024-03-15").date(), expected_cities=("austin", "dallas"), expected_hours=2)
    assert not report["complete"]
    assert report["missing_runs"] == ["2024-03-15"]


def test_archive_audit_accepts_complete_local_runs(tmp_path) -> None:
    _run(tmp_path, "2024-03-14"); _run(tmp_path, "2024-03-15")
    report = audit_archive(roots=(tmp_path,), start=pd.Timestamp("2024-03-14").date(), end=pd.Timestamp("2024-03-15").date(), expected_cities=("austin", "dallas"), expected_hours=2)
    assert report["complete"]
    assert report["present_records"] == 8
