"""Build one immutable, replay-backed candidate training dataset.

This deliberately prepares only data that was available under the original
forecast-issue contracts.  It is not a replacement feature pipeline: Gold
provides genuine ERCOT targets and immutable replay bundles provide the
as-issued demand context and ECMWF IFS weather.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import sys
import tempfile
from pathlib import Path

import pandas as pd

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from scripts.publish_static_dashboard_data import _read_active_delta
from texas_energy.forecast.xgboost_v2 import CAUSAL_DEMAND_FEATURES


HISTORICAL_REPLAY_ISSUE_PREFIX = "202610"
EXPECTED_CITIES = ("austin", "dallas", "el_paso", "houston", "san_antonio")
CONTEXT_HOURS = 2048
HORIZON_HOURS = 24


class CandidateDatasetError(ValueError):
    """A replay source violates the immutable candidate-data contract."""


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _utc(value: object, *, field: str) -> pd.Timestamp:
    parsed = pd.Timestamp(value)
    if parsed.tzinfo is None:
        raise CandidateDatasetError(f"{field} must be timezone-aware UTC.")
    return parsed.tz_convert("UTC")


def _replay_dirs(root: Path) -> list[Path]:
    directories = [path for path in sorted(root.glob("issue=*")) if path.is_dir()]
    selected = [path for path in directories if path.name.removeprefix("issue=").startswith(HISTORICAL_REPLAY_ISSUE_PREFIX)]
    if len(selected) != 14:
        raise CandidateDatasetError("Expected exactly the 14 October 2026 immutable replay bundles.")
    return selected


def _verified_bundle(path: Path, gold: pd.DataFrame) -> tuple[dict[str, object], pd.DataFrame, pd.DataFrame]:
    manifest_path = path / "input_manifest.json"
    demand_path = path / "demand_history.parquet"
    weather_path = path / "weather_forecasts.parquet"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if sha256_file(demand_path) != manifest.get("demand_sha256"):
        raise CandidateDatasetError(f"{path.name} demand checksum differs from its immutable manifest.")
    if sha256_file(weather_path) != manifest.get("weather_sha256"):
        raise CandidateDatasetError(f"{path.name} weather checksum differs from its immutable manifest.")
    issue = _utc(manifest.get("forecast_origin_utc"), field="forecast_origin_utc")
    context_end = _utc(manifest.get("context_end_utc"), field="context_end_utc")
    if context_end != issue - pd.Timedelta(hours=24) or manifest.get("demand_availability_lag_hours") != 24:
        raise CandidateDatasetError(f"{path.name} does not honor the 24-hour demand availability contract.")
    demand = pd.read_parquet(demand_path).copy()
    demand["period_utc"] = pd.to_datetime(demand["period_utc"], utc=True)
    expected_context = pd.date_range(context_end - pd.Timedelta(hours=CONTEXT_HOURS - 1), context_end, freq="h", tz="UTC")
    if list(demand["period_utc"]) != list(expected_context) or demand["observed_average_demand_mw"].isna().any():
        raise CandidateDatasetError(f"{path.name} does not contain an exact finite {CONTEXT_HOURS}-hour demand context.")
    weather = pd.read_parquet(weather_path).copy()
    weather["valid_time_utc"] = pd.to_datetime(weather["valid_time_utc"], utc=True)
    weather["weather_available_at_utc"] = pd.to_datetime(weather["weather_available_at_utc"], utc=True)
    targets = pd.date_range(issue + pd.Timedelta(hours=1), periods=HORIZON_HOURS, freq="h", tz="UTC")
    if len(weather) != len(EXPECTED_CITIES) * HORIZON_HOURS or tuple(sorted(weather["city"].unique())) != EXPECTED_CITIES:
        raise CandidateDatasetError(f"{path.name} does not provide five-city 24-hour issued weather.")
    if weather.duplicated(["city", "valid_time_utc"]).any() or set(weather["valid_time_utc"]) != set(targets):
        raise CandidateDatasetError(f"{path.name} issued weather targets are not exactly the 24 forecast hours.")
    if (weather["weather_available_at_utc"] > issue).any() or weather[["temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh"]].isna().any().any():
        raise CandidateDatasetError(f"{path.name} weather is unavailable at issuance or incomplete.")
    gold_index = gold.set_index("period_utc")["observed_average_demand_mw"]
    if gold_index.reindex(targets).isna().any():
        raise CandidateDatasetError(f"{path.name} forecast targets lack genuine Gold demand observations.")
    expected_gold_context = gold_index.reindex(expected_context)
    if expected_gold_context.isna().any() or not pd.Series(expected_gold_context.to_numpy(float)).equals(pd.Series(demand["observed_average_demand_mw"].to_numpy(float))):
        raise CandidateDatasetError(f"{path.name} captured demand context differs from the active Gold snapshot.")
    return manifest, demand, weather


def _snapshot_id(replay_manifests: dict[str, str], gold: pd.DataFrame) -> str:
    payload = json.dumps({"schema_version": "candidate-training-v1", "replay_manifests": replay_manifests, "gold_rows": gold.to_json(date_format="iso", orient="records")}, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _chronological_split_contract(issues: list[dict[str, object]]) -> dict[str, object]:
    """Define whole-curve partitions without overlapping target timestamps."""

    if len(issues) != 14:
        raise CandidateDatasetError("The bounded candidate contract requires 14 replay issues.")
    origins = [_utc(issue["forecast_origin_utc"], field="forecast_origin_utc") for issue in issues]
    train_target_end = origins[8] + pd.Timedelta(hours=24)
    validation_target_end = origins[10] + pd.Timedelta(hours=24)
    return {
        "train_target_end_utc": train_target_end.isoformat(),
        "validation_target_end_utc": validation_target_end.isoformat(),
        "whole_curve_counts": {"train": 9, "validation": 2, "test": 3},
        "validation_targets_start_strictly_after_train_end": True,
        "test_targets_start_strictly_after_validation_end": True,
    }


def _compact_issue(origin: pd.Timestamp) -> str:
    return origin.strftime("%Y%m%dT%H%M%SZ")


def _full_history_snapshot_id(gold: pd.DataFrame, source_hashes: dict[str, str]) -> str:
    payload = json.dumps({
        "schema_version": "candidate-training-v1-full-history",
        "gold_feature_columns": ["period_utc", "observed_average_demand_mw", "respondent", "demand_unit", *CAUSAL_DEMAND_FEATURES],
        "gold_rows": gold.to_json(date_format="iso", orient="records"),
        "issued_weather_source_hashes": source_hashes,
    }, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]


def _full_history_split_contract(origins: list[dict[str, object]]) -> dict[str, object]:
    """Split complete 24-hour curves chronologically with no target overlap."""

    if len(origins) < 15:
        raise CandidateDatasetError("At least 15 eligible issued-weather origins are required for chronological splits.")
    ordered = sorted(origins, key=lambda item: _utc(item["forecast_origin_utc"], field="forecast_origin_utc"))
    train_count = max(1, int(len(ordered) * 0.60))
    validation_count = max(1, int(len(ordered) * 0.20))
    if train_count + validation_count >= len(ordered):
        raise CandidateDatasetError("Chronological split would leave no held-out test curves.")
    train_last = _utc(ordered[train_count - 1]["forecast_origin_utc"], field="forecast_origin_utc")
    validation_first = _utc(ordered[train_count]["forecast_origin_utc"], field="forecast_origin_utc")
    validation_last = _utc(ordered[train_count + validation_count - 1]["forecast_origin_utc"], field="forecast_origin_utc")
    test_first = _utc(ordered[train_count + validation_count]["forecast_origin_utc"], field="forecast_origin_utc")
    if validation_first < train_last + pd.Timedelta(hours=24) or test_first < validation_last + pd.Timedelta(hours=24):
        raise CandidateDatasetError("Issued-weather origins would overlap forecast targets across chronological splits.")
    return {
        "schema_version": "whole-curve-chronological-split-v1",
        "train_target_end_utc": (train_last + pd.Timedelta(hours=24)).isoformat(),
        "validation_target_end_utc": (validation_last + pd.Timedelta(hours=24)).isoformat(),
        "test_target_start_utc": (test_first + pd.Timedelta(hours=1)).isoformat(),
        "whole_curve_counts": {"train": train_count, "validation": validation_count, "test": len(ordered) - train_count - validation_count},
        "target_overlap_allowed": False,
    }


def build_full_history_dataset(
    *, gold_demand_delta_root: Path, issued_weather_roots: tuple[Path, ...], output_root: Path
) -> Path:
    """Stage causal full-history demand and issued weather for candidate training.

    Historical observed weather in the source Gold snapshot is deliberately not
    carried into this dataset.  The curve trainers receive only demand context
    plus weather that was explicitly issued and available at each origin.
    """

    gold = _read_active_delta(gold_demand_delta_root).copy()
    required = {"period_utc", "observed_average_demand_mw", "respondent", "demand_unit", *CAUSAL_DEMAND_FEATURES}
    if missing := required.difference(gold.columns):
        raise CandidateDatasetError(f"Gold snapshot lacks required demand columns: {sorted(missing)}")
    gold["period_utc"] = pd.to_datetime(gold["period_utc"], utc=True)
    gold = gold.sort_values("period_utc").drop_duplicates("period_utc", keep="last").reset_index(drop=True)
    if gold["observed_average_demand_mw"].isna().any():
        raise CandidateDatasetError("Gold demand snapshot contains null observed targets.")
    demand = gold[["period_utc", "observed_average_demand_mw", "respondent", "demand_unit", *CAUSAL_DEMAND_FEATURES]].copy()
    demand_index = demand.set_index("period_utc")["observed_average_demand_mw"]
    files = [path for root in issued_weather_roots for path in sorted(root.glob("**/weather_forecasts.parquet"))]
    if not files:
        raise CandidateDatasetError("No immutable issued-weather source files were found.")
    source_hashes = {str(path): sha256_file(path) for path in files}
    eligible: list[tuple[str, pd.Timestamp, pd.DataFrame, str]] = []
    seen_origins: set[pd.Timestamp] = set()
    for path in files:
        weather = pd.read_parquet(path).copy()
        required_weather = {
            "city", "valid_time_utc", "weather_model_run_utc", "weather_available_at_utc",
            "temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh",
        }
        if missing := required_weather.difference(weather.columns):
            raise CandidateDatasetError(f"{path} lacks issued-weather columns: {sorted(missing)}")
        weather["valid_time_utc"] = pd.to_datetime(weather["valid_time_utc"], utc=True)
        weather["weather_available_at_utc"] = pd.to_datetime(weather["weather_available_at_utc"], utc=True)
        origins = weather["weather_available_at_utc"].drop_duplicates().tolist()
        if len(origins) != 1:
            raise CandidateDatasetError(f"{path} must represent exactly one immutable weather origin.")
        origin = origins[0]
        if origin in seen_origins:
            raise CandidateDatasetError(f"Duplicate immutable issued-weather origin: {origin.isoformat()}")
        seen_origins.add(origin)
        targets = pd.date_range(origin + pd.Timedelta(hours=1), periods=HORIZON_HOURS, freq="h", tz="UTC")
        window = weather.loc[weather["valid_time_utc"].isin(targets)].copy()
        weather_complete = (
            len(window) == len(EXPECTED_CITIES) * HORIZON_HOURS
            and tuple(sorted(window["city"].unique())) == EXPECTED_CITIES
            and not window.duplicated(["city", "valid_time_utc"]).any()
            and set(window["valid_time_utc"]) == set(targets)
            and not window[["temperature_2m_c", "relative_humidity_pct", "apparent_temperature_c", "wind_speed_10m_kmh"]].isna().any().any()
            and not (window["weather_available_at_utc"] > origin).any()
        )
        context_end = origin - pd.Timedelta(hours=24)
        context = pd.date_range(context_end - pd.Timedelta(hours=167), context_end, freq="h", tz="UTC")
        demand_complete = not demand_index.reindex(context.union(targets)).isna().any()
        if weather_complete and demand_complete:
            eligible.append((_compact_issue(origin), origin, window, source_hashes[str(path)]))
    eligible.sort(key=lambda item: item[1])
    if len(eligible) < 15:
        raise CandidateDatasetError("Fewer than 15 causal issued-weather origins remain after data validation.")
    output_root.mkdir(parents=True, exist_ok=True)
    dataset_id = _full_history_snapshot_id(demand, source_hashes)
    destination = output_root / f"dataset={dataset_id}"
    if destination.exists():
        raise CandidateDatasetError(f"Candidate dataset already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix="candidate-training-", dir=output_root) as temporary:
        stage = Path(temporary)
        (stage / "gold").mkdir(); (stage / "weather").mkdir(); (stage / "manifest").mkdir()
        demand.to_parquet(stage / "gold" / "gold.parquet", index=False)
        origins: list[dict[str, object]] = []
        for issue, origin, weather, source_hash in eligible:
            target = stage / "weather" / f"run={issue}"; target.mkdir()
            weather.to_parquet(target / "weather_forecasts.parquet", index=False)
            origins.append({
                "issue_id": issue, "forecast_origin_utc": origin.isoformat(),
                "context_end_utc": (origin - pd.Timedelta(hours=24)).isoformat(),
                "source_weather_sha256": source_hash, "genuine_gold_target_hours": HORIZON_HOURS,
            })
        artifacts = {str(path.relative_to(stage)).replace("\\", "/"): sha256_file(path) for path in sorted(stage.rglob("*.parquet"))}
        split_contract = _full_history_split_contract(origins)
        manifest = {
            "schema_version": "candidate-training-v2-full-history", "dataset_id": dataset_id,
            "promotion_status": "candidate_only_not_automatically_promotable",
            "quality_assessment": {
                "gold_demand_hours": int(len(demand)), "issued_weather_source_origins": len(files),
                "causal_eligible_origins": len(origins),
                "gold_observed_through_utc": demand["period_utc"].max().isoformat(),
                "latest_eligible_forecast_origin_utc": origins[-1]["forecast_origin_utc"],
                "training_conclusion": "Full-history candidate dataset for bounded model training and chronological evaluation.",
            },
            "causality": {
                "demand_availability_lag_hours": 24, "demand_context_hours": 168,
                "weather_source": "immutable ECMWF IFS issued-weather archive",
                "observed_weather_excluded_from_features": True,
                "training_targets": "genuine historical ERCOT demand only",
                "split_requirement": "whole 24-hour curves in strictly chronological non-overlapping target windows",
            },
            "chronological_split_contract": split_contract,
            "source_inventory": [{"source_path": path, "sha256": source_hashes[path]} for path in sorted(source_hashes)],
            "origins": origins, "files_sha256": artifacts,
        }
        (stage / "manifest" / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        shutil.move(str(stage), str(destination))
    return destination


def build_dataset(*, replay_root: Path, gold_demand_delta_root: Path, output_root: Path) -> Path:
    """Create a new local immutable candidate directory and return it."""

    gold = _read_active_delta(gold_demand_delta_root).copy()
    required = {"period_utc", "observed_average_demand_mw", "respondent", "demand_unit"}
    if missing := required.difference(gold.columns):
        raise CandidateDatasetError(f"Gold snapshot lacks required demand columns: {sorted(missing)}")
    gold["period_utc"] = pd.to_datetime(gold["period_utc"], utc=True)
    gold = gold.sort_values("period_utc").drop_duplicates("period_utc", keep="last").reset_index(drop=True)
    if gold["observed_average_demand_mw"].isna().any() or gold["period_utc"].diff().dropna().ne(pd.Timedelta(hours=1)).any():
        raise CandidateDatasetError("Gold demand snapshot must be finite and hourly-continuous.")
    bundles = _replay_dirs(replay_root)
    checked: list[tuple[Path, dict[str, object], pd.DataFrame, pd.DataFrame]] = []
    for bundle in bundles:
        manifest, demand, weather = _verified_bundle(bundle, gold)
        checked.append((bundle, manifest, demand, weather))
    output_root.mkdir(parents=True, exist_ok=True)
    replay_hashes = {bundle.name: sha256_file(bundle / "input_manifest.json") for bundle, _, _, _ in checked}
    dataset_id = _snapshot_id(replay_hashes, gold)
    destination = output_root / f"dataset={dataset_id}"
    if destination.exists():
        raise CandidateDatasetError(f"Candidate dataset already exists: {destination}")
    with tempfile.TemporaryDirectory(prefix="candidate-training-", dir=output_root) as temporary:
        stage = Path(temporary)
        (stage / "gold").mkdir()
        (stage / "weather").mkdir()
        (stage / "manifest").mkdir()
        gold.to_parquet(stage / "gold" / "gold.parquet", index=False)
        issues: list[dict[str, object]] = []
        for bundle, manifest, _, weather in checked:
            issue = bundle.name.removeprefix("issue=")
            target = stage / "weather" / f"run={issue}"
            target.mkdir()
            weather.to_parquet(target / "weather_forecasts.parquet", index=False)
            issues.append({
                "issue_id": issue,
                "forecast_origin_utc": str(manifest["forecast_origin_utc"]),
                "context_end_utc": str(manifest["context_end_utc"]),
                "source_manifest_sha256": replay_hashes[bundle.name],
                "source_demand_sha256": str(manifest["demand_sha256"]),
                "source_weather_sha256": str(manifest["weather_sha256"]),
                "genuine_gold_target_hours": HORIZON_HOURS,
            })
        artifacts = {str(path.relative_to(stage)).replace("\\", "/"): sha256_file(path) for path in sorted(stage.rglob("*.parquet"))}
        manifest = {
            "schema_version": "candidate-training-v1",
            "dataset_id": dataset_id,
            "promotion_status": "smoke_only_not_production_eligible",
            "quality_assessment": {
                "gold_demand_hours": int(len(gold)),
                "gold_start_utc": gold["period_utc"].min().isoformat(),
                "gold_end_utc": gold["period_utc"].max().isoformat(),
                "issued_weather_origins": len(issues),
                "training_conclusion": "The 14 archived issued-weather curves are suitable only for contract and CPU smoke validation, not production-quality model selection or promotion.",
            },
            "causality": {
                "demand_availability_lag_hours": 24,
                "demand_context_hours": CONTEXT_HOURS,
                "weather_source": "immutable ECMWF IFS issued-weather replay bundles",
                "weather_must_be_available_at_or_before_forecast_origin": True,
                "training_targets": "genuine canonical Gold EIA ERCOT observed demand only",
                "split_requirement": "whole 24-hour curves in strictly chronological non-overlapping target windows",
            },
            "chronological_split_contract": _chronological_split_contract(issues),
            "issues": issues,
            "files_sha256": artifacts,
        }
        (stage / "manifest" / "dataset_manifest.json").write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        shutil.move(str(stage), str(destination))
    return destination


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--replay-root", type=Path)
    parser.add_argument("--issued-weather-root", type=Path, nargs="+")
    parser.add_argument("--gold-demand-delta-root", type=Path, required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    arguments = parser.parse_args()
    if bool(arguments.replay_root) == bool(arguments.issued_weather_root):
        parser.error("Supply exactly one of --replay-root or --issued-weather-root.")
    if arguments.replay_root:
        dataset = build_dataset(replay_root=arguments.replay_root, gold_demand_delta_root=arguments.gold_demand_delta_root, output_root=arguments.output_root)
    else:
        dataset = build_full_history_dataset(gold_demand_delta_root=arguments.gold_demand_delta_root, issued_weather_roots=tuple(arguments.issued_weather_root), output_root=arguments.output_root)
    manifest = dataset / "manifest" / "dataset_manifest.json"
    print(json.dumps({"dataset": str(dataset), "manifest_sha256": sha256_file(manifest)}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
