"""Immutable, local-only contracts for prospective 24-hour forecast trials."""

from __future__ import annotations

import hashlib
import json
import math
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

import pandas as pd

from .curve_contract import validate_forecast_curve
from .point_in_time import DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS
from .weather_features import build_point_in_time_weather_features


CITIES = ("austin", "dallas", "el_paso", "houston", "san_antonio")
INPUT_MANIFEST = "input_manifest.json"
DEMAND_FILE = "demand_history.parquet"
WEATHER_FILE = "weather_forecasts.parquet"
FORECAST_FILE = "forecast_predictions.parquet"
ISSUE_MANIFEST = "issue_manifest.json"


class ProspectiveTrialError(ValueError):
    """A trial cannot prove causal, complete, immutable inputs or outputs."""


def _utc(value: object, *, field: str) -> pd.Timestamp:
    parsed = pd.to_datetime(value, utc=True, errors="coerce")
    if pd.isna(parsed) or parsed != parsed.floor("h"):
        raise ProspectiveTrialError(f"{field} must be an exact UTC hour.")
    return parsed


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _atomic_bytes(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, prefix=f".{path.name}.", delete=False) as handle:
        temporary = Path(handle.name)
        handle.write(data); handle.flush(); os.fsync(handle.fileno())
    try:
        temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


def atomic_json(path: Path, payload: Mapping[str, Any]) -> None:
    _atomic_bytes(path, (json.dumps(payload, indent=2, sort_keys=True, allow_nan=False) + "\n").encode("utf-8"))


def atomic_parquet(path: Path, frame: pd.DataFrame) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=path.parent, suffix=".parquet", delete=False) as handle:
        temporary = Path(handle.name)
    try:
        frame.to_parquet(temporary, index=False); temporary.replace(path)
    finally:
        temporary.unlink(missing_ok=True)


@dataclass(frozen=True)
class ProspectiveInputs:
    issue_utc: pd.Timestamp
    context_end_utc: pd.Timestamp
    demand: pd.DataFrame
    weather: pd.DataFrame
    manifest: Mapping[str, Any]
    bundle_path: Path


def validate_input_bundle(path: Path, *, context_hours: int = 2048) -> ProspectiveInputs:
    """Read a captured bundle and verify the strictest (Chronos) causal input."""

    manifest_path, demand_path, weather_path = path / INPUT_MANIFEST, path / DEMAND_FILE, path / WEATHER_FILE
    if not all(item.is_file() for item in (manifest_path, demand_path, weather_path)):
        raise ProspectiveTrialError("Input bundle is missing its immutable manifest, demand, or weather file.")
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise ProspectiveTrialError("Input bundle manifest is not valid JSON.") from None
    if manifest.get("schema_version") != "1.0":
        raise ProspectiveTrialError("Unsupported prospective input bundle schema.")
    if manifest.get("generation_type") in {"aws_managed_prospective_input", "aws_managed_historical_replay_input"}:
        gold_uri = manifest.get("gold_demand_features_uri")
        if not isinstance(gold_uri, str) or not gold_uri.startswith("s3://"):
            raise ProspectiveTrialError("AWS-managed input must identify its Gold demand source.")
        if "silver_demand_uri" in manifest:
            raise ProspectiveTrialError("AWS-managed input cannot use Silver as its demand authority.")
    if manifest.get("demand_sha256") != sha256_file(demand_path) or manifest.get("weather_sha256") != sha256_file(weather_path):
        raise ProspectiveTrialError("Input bundle checksum does not match its manifest.")
    issue = _utc(manifest.get("forecast_origin_utc"), field="forecast_origin_utc")
    context_end = issue - pd.Timedelta(hours=DEFAULT_DEMAND_AVAILABILITY_LAG_HOURS)
    if _utc(manifest.get("context_end_utc"), field="context_end_utc") != context_end:
        raise ProspectiveTrialError("Input bundle context end does not match the configured 24-hour demand lag.")
    demand = pd.read_parquet(demand_path).copy()
    required_demand = {"period_utc", "observed_average_demand_mw", "respondent", "demand_type"}
    if missing := required_demand.difference(demand.columns):
        raise ProspectiveTrialError(f"Demand input lacks required columns: {sorted(missing)}")
    # The first managed bundle revision retained Gold's demand_unit instead of
    # the intended normalized source_unit.  Both names mean the same explicit
    # EIA contract and must still carry the canonical MWh value; accepting the
    # former makes that immutable bundle usable without rewriting it.
    unit_column = "source_unit" if "source_unit" in demand.columns else "demand_unit"
    if unit_column not in demand.columns:
        raise ProspectiveTrialError("Demand input lacks its canonical source unit.")
    demand["period_utc"] = pd.to_datetime(demand["period_utc"], utc=True, errors="coerce")
    demand["observed_average_demand_mw"] = pd.to_numeric(demand["observed_average_demand_mw"], errors="coerce")
    if demand["period_utc"].isna().any() or demand["period_utc"].duplicated().any() or demand["observed_average_demand_mw"].isna().any() or not demand["observed_average_demand_mw"].map(math.isfinite).all():
        raise ProspectiveTrialError("Demand input has invalid, duplicate, or null hourly observations.")
    if not demand["respondent"].eq("ERCO").all() or not demand["demand_type"].eq("D").all() or not demand[unit_column].eq("megawatthours").all():
        raise ProspectiveTrialError("Demand input is not the canonical ERCO/D hourly-megawatthours contract.")
    demand_index = demand.set_index("period_utc").sort_index()
    required_hours = pd.date_range(context_end - pd.Timedelta(hours=context_hours - 1), context_end, freq="h", tz="UTC")
    if not required_hours.isin(demand_index.index).all():
        raise ProspectiveTrialError("Demand input lacks continuous history through the allowed context end.")
    weather = pd.read_parquet(weather_path).copy()
    required_weather = {"city", "valid_time_utc", "weather_model_run_utc", "weather_available_at_utc", "retrieval_time_utc", "availability_basis", "temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh", "temperature_2m_unit", "relative_humidity_unit", "apparent_temperature_unit", "wind_speed_10m_unit"}
    if missing := required_weather.difference(weather.columns):
        raise ProspectiveTrialError(f"Weather input lacks required columns: {sorted(missing)}")
    for column in ("valid_time_utc", "weather_model_run_utc", "weather_available_at_utc", "retrieval_time_utc"):
        weather[column] = pd.to_datetime(weather[column], utc=True, errors="coerce")
    weather_values = ("temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh")
    for column in weather_values:
        weather[column] = pd.to_numeric(weather[column], errors="coerce")
    targets = issue + pd.to_timedelta(range(1, 25), unit="h")
    weather = weather.loc[weather["valid_time_utc"].isin(targets)].copy()
    if set(weather["city"]) != set(CITIES) or len(weather) != len(CITIES) * 24:
        raise ProspectiveTrialError("Weather input must contain five cities for every requested target hour.")
    finite_weather = weather[list(weather_values)].apply(lambda column: column.map(math.isfinite)).all().all()
    if weather.duplicated(["city", "valid_time_utc"]).any() or (weather["weather_available_at_utc"] > issue).any() or weather[list(weather_values)].isna().any().any() or not finite_weather:
        raise ProspectiveTrialError("Weather input is duplicate or was not available at issuance.")
    return ProspectiveInputs(issue, context_end, demand_index.reset_index(), weather, manifest, path)


def xgboost_feature_frame(inputs: ProspectiveInputs) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Build direct XGBoost curve features using only captured, pre-issue data."""

    indexed = inputs.demand.set_index("period_utc").sort_index()["observed_average_demand_mw"]
    end = inputs.context_end_utc
    def value(hours: int) -> float:
        result = indexed.get(end - pd.Timedelta(hours=hours))
        if result is None or not math.isfinite(float(result)):
            raise ProspectiveTrialError("Demand history cannot form a required causal lag.")
        return float(result)
    def mean(hours: int) -> float:
        values = indexed.reindex(pd.date_range(end - pd.Timedelta(hours=hours), end - pd.Timedelta(hours=1), freq="h", tz="UTC"))
        if values.isna().any():
            raise ProspectiveTrialError("Demand history cannot form a complete causal rolling window.")
        return float(values.mean())
    targets = inputs.issue_utc + pd.to_timedelta(range(1, 25), unit="h")
    rows = pd.DataFrame({"forecast_origin_utc": inputs.issue_utc, "context_end_utc": end, "target_timestamp_utc": targets, "horizon_hours": range(1, 25), "context_demand_lag_1h_mwh": value(1), "context_demand_lag_24h_mwh": value(24), "context_demand_lag_168h_mwh": value(168), "context_demand_trailing_mean_24h_mwh": mean(24), "context_demand_trailing_mean_168h_mwh": mean(168), "context_observed_average_demand_mw": value(0)})
    rows["target_utc_hour"] = rows["target_timestamp_utc"].dt.hour.astype("int8")
    rows["target_utc_day_of_week_sun1"] = ((rows["target_timestamp_utc"].dt.dayofweek + 1) % 7 + 1).astype("int8")
    rows["target_utc_month"] = rows["target_timestamp_utc"].dt.month.astype("int8")
    rows["target_is_weekend_utc"] = (rows["target_timestamp_utc"].dt.dayofweek >= 5).astype("int8")
    joined = build_point_in_time_weather_features(rows, inputs.weather, expected_cities=CITIES)
    return joined.features, joined.selected_weather


def validate_prediction_frame(frame: pd.DataFrame, *, model_id: str, inputs: ProspectiveInputs) -> pd.DataFrame:
    result = frame.copy(); result["model_id"] = model_id; result["forecast_origin_utc"] = inputs.issue_utc; result["feature_as_of_utc"] = inputs.context_end_utc
    try:
        checked = validate_forecast_curve(result)
    except ValueError as error:
        raise ProspectiveTrialError("Forecast output did not satisfy the complete causal curve contract.") from None
    if len(checked) != 24:
        raise ProspectiveTrialError("A prospective model must emit exactly 24 predictions.")
    return checked


def issue_directory(root: Path, issue_utc: pd.Timestamp) -> Path:
    return root / f"issue={issue_utc.strftime('%Y%m%dT%H%M%SZ')}"


def acquire_trial_lock(root: Path) -> Path:
    root.mkdir(parents=True, exist_ok=True); lock = root / ".prospective-trial.lock"
    try:
        lock.mkdir()
    except FileExistsError:
        raise ProspectiveTrialError("A prospective-trial lock already exists; inspect it before retrying.") from None
    atomic_json(lock / "owner.json", {"pid": os.getpid(), "created_at_utc": pd.Timestamp.now(tz="UTC").isoformat()})
    return lock


def release_trial_lock(lock: Path) -> None:
    for item in lock.iterdir(): item.unlink(missing_ok=True)
    lock.rmdir()


def completed_issue_count(root: Path) -> int:
    total = 0
    for manifest in root.glob("issue=*/" + ISSUE_MANIFEST):
        try: total += json.loads(manifest.read_text(encoding="utf-8")).get("status") == "complete"
        except (OSError, json.JSONDecodeError): pass
    return total
