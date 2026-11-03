"""Local integrity checks for immutable issued-weather archive runs."""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import pandas as pd


class IssuedWeatherArchiveError(ValueError):
    """The local archive is incomplete or not shaped like a usable run set."""


def expected_run_days(*, start: date, end: date) -> tuple[date, ...]:
    if end < start:
        raise IssuedWeatherArchiveError("end must not precede start.")
    return tuple(start + timedelta(days=index) for index in range((end - start).days + 1))


def audit_archive(*, roots: tuple[Path, ...], start: date, end: date, expected_cities: tuple[str, ...], expected_hours: int = 31) -> dict[str, object]:
    """Validate run identity and balanced city/hour coverage without mutation."""

    by_day: dict[date, Path] = {}
    duplicate_days: list[str] = []
    invalid_runs: list[dict[str, str]] = []
    for root in roots:
        for run in sorted(root.glob("run=*T00Z")):
            try:
                day = date.fromisoformat(run.name.removeprefix("run=").removesuffix("T00Z"))
            except ValueError:
                invalid_runs.append({"run": str(run), "reason": "invalid_run_directory"})
                continue
            if day in by_day:
                duplicate_days.append(day.isoformat())
                continue
            parquet, manifest = run / "weather_forecasts.parquet", run / "manifest.json"
            if not parquet.is_file() or not manifest.is_file():
                invalid_runs.append({"run": str(run), "reason": "missing_manifest_or_parquet"})
                continue
            frame = pd.read_parquet(parquet)
            required = {"city", "valid_time_utc", "weather_model_run_utc", "weather_available_at_utc"}
            if missing := required.difference(frame.columns):
                invalid_runs.append({"run": str(run), "reason": f"missing_columns:{sorted(missing)}"})
                continue
            frame["valid_time_utc"] = pd.to_datetime(frame["valid_time_utc"], utc=True, errors="coerce")
            frame["weather_model_run_utc"] = pd.to_datetime(frame["weather_model_run_utc"], utc=True, errors="coerce")
            frame["weather_available_at_utc"] = pd.to_datetime(frame["weather_available_at_utc"], utc=True, errors="coerce")
            expected_rows = len(expected_cities) * expected_hours
            balanced = len(frame) == expected_rows and set(frame["city"]) == set(expected_cities)
            run_utc = pd.Timestamp(f"{day.isoformat()}T00:00:00Z")
            valid_leads = (frame["valid_time_utc"] - run_utc).dt.total_seconds().div(3600)
            # ``weather_forecast_issue_utc`` is intentionally unknown for
            # historical Single Runs.  Validate only the required identity
            # and provider-value fields; requiring every optional lineage
            # field to be non-null would falsely reject a valid archive.
            required_values = [
                "city", "valid_time_utc", "weather_model_run_utc", "weather_available_at_utc",
                "temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh",
            ]
            if not balanced or frame[required_values].isna().any().any() or frame.duplicated(["city", "valid_time_utc"]).any() or valid_leads.nunique() != expected_hours:
                invalid_runs.append({"run": str(run), "reason": "unbalanced_or_invalid_city_hour_coverage"})
                continue
            by_day[day] = run
    expected = set(expected_run_days(start=start, end=end))
    present = set(by_day)
    missing = sorted(day.isoformat() for day in expected - present)
    extra = sorted(day.isoformat() for day in present - expected)
    return {
        "expected_runs": len(expected), "present_runs": len(present), "missing_runs": missing,
        "extra_runs": extra, "duplicate_run_days": sorted(set(duplicate_days)), "invalid_runs": invalid_runs,
        "complete": not missing and not extra and not duplicate_days and not invalid_runs,
        "expected_records": len(expected) * len(expected_cities) * expected_hours,
        "present_records": len(present) * len(expected_cities) * expected_hours,
    }
