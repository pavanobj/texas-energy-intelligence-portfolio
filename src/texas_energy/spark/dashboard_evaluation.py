"""Publish static prospective forecast evaluation from immutable lake products.

This module intentionally runs inside the existing production Glue job.  It never
creates or changes a forecast, Silver, or Gold table: it reads immutable model
outputs and the current Gold observations, then replaces a small static dashboard
projection only after the complete candidate passes its contract checks.
"""

from __future__ import annotations

import hashlib
import io
import json
import math
import re
import tempfile
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlparse


_MODELS = ("xgboost", "bilstm", "chronos2")
_MODEL_LABELS = {
    "xgboost": "Weather-aware XGBoost",
    "bilstm": "Weather-aware Bi-LSTM",
    "chronos2": "Chronos-2 zero-shot",
}
_MODEL_IDS = {
    "xgboost": "weather_aware_xgboost_curve",
    "bilstm": "weather_aware_bilstm_curve",
    "chronos2": "amazon/chronos-2",
}
_LINEAGE_PATTERN = re.compile(r"^issue=(?P<issue>[^/]+)/model=(?P<model>[^/]+)/run=(?P<run>[^/]+)/lineage\.json$")


def _s3_parts(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path:
        raise ValueError(f"Expected a complete S3 URI, got {uri!r}")
    return parsed.netloc, parsed.path.lstrip("/").rstrip("/")


# Old version _iso.
# def _iso(value: Any) -> str:
#     converted = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value
#     if converted.tzinfo is None:
#         converted = converted.replace(tzinfo=timezone.utc)
#     return converted.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _iso(value: Any) -> str:
    converted = value.to_pydatetime() if hasattr(value, "to_pydatetime") else value

    if isinstance(converted, str):
        converted = datetime.fromisoformat(
            converted.replace("Z", "+00:00")
        )

    if not isinstance(converted, datetime):
        raise TypeError(f"Unsupported timestamp type: {type(value).__name__}")

    if converted.tzinfo is None:
        converted = converted.replace(tzinfo=timezone.utc)

    return converted.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")

def _finite_or_none(value: Any) -> float | None:
    if value is None:
        return None
    number = float(value)
    return number if math.isfinite(number) else None


def _metrics(rows: list[dict[str, Any]]) -> dict[str, Any]:
    pairs = [(row["predicted_demand_mw"], row["actual_demand_mw"]) for row in rows if row["actual_demand_mw"] is not None]
    errors = [prediction - actual for prediction, actual in pairs]
    return {
        "matched_observation_count": len(pairs),
        "missing_observation_count": len(rows) - len(pairs),
        "missing_prediction_count": sum(row["predicted_demand_mw"] is None for row in rows),
        "mae_mw": sum(abs(error) for error in errors) / len(errors) if errors else None,
        "rmse_mw": math.sqrt(sum(error * error for error in errors) / len(errors)) if errors else None,
        "bias_mw": sum(errors) / len(errors) if errors else None,
    }


def _validate_curve(issue_id: str, model: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    ordered = sorted(rows, key=lambda row: row["horizon_hours"])
    if len(ordered) != 24 or [row["horizon_hours"] for row in ordered] != list(range(1, 25)):
        raise ValueError(f"{issue_id}/{model} must contain exactly horizons 1..24")
    targets = [row["target_timestamp_utc"] for row in ordered]
    if len(set(targets)) != 24:
        raise ValueError(f"{issue_id}/{model} has duplicate target timestamps")
    if any(row["predicted_demand_mw"] is None for row in ordered):
        raise ValueError(f"{issue_id}/{model} contains a non-finite prediction")
    origin = ordered[0]["forecast_origin_utc"]
    expected = datetime.fromisoformat(origin.replace("Z", "+00:00"))
    for row in ordered:
        expected_target = expected.timestamp() + row["horizon_hours"] * 3600
        if abs(datetime.fromisoformat(row["target_timestamp_utc"].replace("Z", "+00:00")).timestamp() - expected_target) > 0.1:
            raise ValueError(f"{issue_id}/{model} target timestamps are not hourly from the issue")
    return ordered


def _s3_client() -> Any:
    import boto3

    return boto3.client("s3")


def _candidate_outputs(forecast_output: str, client: Any) -> list[dict[str, str]]:
    """Return only issues with exactly one immutable lineage per required model."""
    bucket, prefix = _s3_parts(forecast_output)
    candidates: dict[str, dict[str, list[dict[str, str]]]] = defaultdict(lambda: defaultdict(list))
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for item in page.get("Contents", []):
            match = _LINEAGE_PATTERN.match(item["Key"].removeprefix(f"{prefix}/"))
            if match and match["model"] in _MODELS:
                entry = match.groupdict()
                entry["lineage_key"] = item["Key"]
                entry["prediction_key"] = item["Key"].removesuffix("lineage.json") + "predictions.parquet"
                candidates[entry["issue"]][entry["model"]].append(entry)
    selected: list[dict[str, str]] = []
    for issue, by_model in candidates.items():
        if set(by_model) != set(_MODELS) or any(len(by_model[model]) != 1 for model in _MODELS):
            continue
        selected.extend(by_model[model][0] for model in _MODELS)
    return sorted(selected, key=lambda item: (item["issue"], item["model"]))


def _read_json(bucket: str, key: str, client: Any) -> dict[str, Any]:
    with client.get_object(Bucket=bucket, Key=key)["Body"] as body:
        return json.loads(body.read().decode("utf-8"))


def _put_json(bucket: str, key: str, payload: dict[str, Any], client: Any, *, immutable: bool = False) -> str:
    encoded = (json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
    request: dict[str, Any] = {
        "Bucket": bucket,
        "Key": key,
        "Body": encoded,
        "ContentType": "application/json",
        "CacheControl": "max-age=60",
        "ServerSideEncryption": "AES256",
        "Metadata": {"sha256": hashlib.sha256(encoded).hexdigest()},
    }
    if immutable:
        # Glue's bundled botocore model does not expose PutObject's REST
        # If-None-Match header.  Refresh paths are unique and the job permits
        # one concurrent run, so reject a pre-existing key before writing.
        try:
            client.head_object(Bucket=bucket, Key=key)
        except Exception as error:  # boto3 reports a missing object as ClientError/404.
            code = str(getattr(error, "response", {}).get("Error", {}).get("Code", ""))
            if code not in {"404", "NoSuchKey", "NotFound"}:
                raise
        else:
            raise ValueError(f"Immutable dashboard snapshot already exists: s3://{bucket}/{key}")
    client.put_object(**request)
    return request["Metadata"]["sha256"]


def _forecast_rows(bucket: str, outputs: list[dict[str, str]], actuals: dict[str, float], client: Any) -> tuple[dict[str, dict[str, list[dict[str, Any]]]], dict[str, dict[str, Any]]]:
    """Read tiny immutable model files with Arrow; Spark cannot decode TIMESTAMP(NANOS)."""
    from pyarrow import parquet

    curves: dict[str, dict[str, list[dict[str, Any]]]] = defaultdict(lambda: defaultdict(list))
    lineage: dict[str, dict[str, Any]] = {}
    columns = ["horizon_hours", "target_timestamp_utc", "predicted_demand_mw", "model_id", "run_id", "forecast_origin_utc", "feature_as_of_utc", "forecast_generation_type", "demand_unit"]
    for output in outputs:
        with client.get_object(Bucket=bucket, Key=output["prediction_key"])["Body"] as body:
            rows = parquet.read_table(io.BytesIO(body.read()), columns=columns).to_pylist()
        issue, model = output["issue"], output["model"]
        for row in rows:
            if row["model_id"] != _MODEL_IDS[model]:
                raise ValueError(f"{issue}/{model} model identifier does not match its immutable output prefix")
            target = _iso(row["target_timestamp_utc"])
            curves[issue][model].append({
                "horizon_hours": int(row["horizon_hours"]), "target_timestamp_utc": target,
                "forecast_origin_utc": _iso(row["forecast_origin_utc"]), "predicted_demand_mw": _finite_or_none(row["predicted_demand_mw"]),
                "actual_demand_mw": actuals.get(target), "demand_unit": row["demand_unit"],
            })
            lineage[f"{issue}/{model}"] = {"model_id": row["model_id"], "run_id": row["run_id"], "feature_as_of_utc": _iso(row["feature_as_of_utc"]), "generation_type": row["forecast_generation_type"]}
    return curves, lineage


def _arrow_rows(bucket: str, keys: list[str], columns: list[str], client: Any) -> list[dict[str, Any]]:
    from pyarrow import parquet

    rows: list[dict[str, Any]] = []
    for key in keys:
        with client.get_object(Bucket=bucket, Key=key)["Body"] as body:
            rows.extend(parquet.read_table(io.BytesIO(body.read()), columns=columns).to_pylist())
    return rows


def _materialize_publisher_delta_table(table_uri: str, destination: Any, client: Any) -> Any:
    """Stage a small Gold table locally for the existing proven dashboard reader."""
    bucket, prefix = _s3_parts(table_uri)
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=bucket, Prefix=f"{prefix}/"):
        for item in page.get("Contents", []):
            key = item["Key"]
            if not (key.endswith(".json") or key.endswith(".parquet")):
                continue
            local = destination / key.removeprefix(f"{prefix}/")
            local.parent.mkdir(parents=True, exist_ok=True)
            with client.get_object(Bucket=bucket, Key=key)["Body"] as body:
                local.write_bytes(body.read())
    return destination


def _gold_actuals(gold_output: str, client: Any) -> dict[str, float]:
    from pathlib import Path
    from publish_static_dashboard_data import _gold_actuals as publisher_gold_actuals

    with tempfile.TemporaryDirectory(prefix="tei-dashboard-demand-") as workspace:
        frame = publisher_gold_actuals(_materialize_publisher_delta_table(gold_output, Path(workspace), client))
    return {
        _iso(row.period_utc): float(row.observed_average_demand_mw)
        for row in frame.itertuples()
        if row.respondent == "ERCO" and _finite_or_none(row.observed_average_demand_mw) is not None
    }


def _weather_records(input_root: str, weather_gold: str, issues: set[str], client: Any) -> tuple[dict[str, list[dict[str, Any]]], dict[str, Any]]:
    input_bucket, input_prefix = _s3_parts(input_root)
    records: dict[str, list[dict[str, Any]]] = defaultdict(list)
    columns = ["city", "valid_time_utc", "weather_model_run_utc", "weather_available_at_utc", "temperature_2m_c", "relative_humidity_pct", "wind_speed_10m_kmh"]
    issued = {
        issue: _arrow_rows(input_bucket, [f"{input_prefix}/issue={issue}/weather_forecasts.parquet"], columns, client)
        for issue in sorted(issues)
    }
    targets = [row["valid_time_utc"] for rows in issued.values() for row in rows]
    from pathlib import Path
    from publish_static_dashboard_data import _observed_weather as publisher_observed_weather
    with tempfile.TemporaryDirectory(prefix="tei-dashboard-weather-") as workspace:
        observed_rows = publisher_observed_weather(
            _materialize_publisher_delta_table(weather_gold, Path(workspace), client), targets
        )
    observed = {(_iso(row["target_timestamp_utc"]), row["city"]): row for row in observed_rows}
    for issue in sorted(issues):
        for row in issued[issue]:
            target = _iso(row["valid_time_utc"])
            observation = observed.get((target, row["city"].replace("_", " ").title()))
            records[issue].append({
                "issue_id": issue, "city": row["city"], "target_timestamp_utc": target, "issued_temperature_2m_c": _finite_or_none(row["temperature_2m_c"]),
                "relative_humidity_pct": _finite_or_none(row["relative_humidity_pct"]), "wind_speed_10m_kmh": _finite_or_none(row["wind_speed_10m_kmh"]), "observed_temperature_2m_c": _finite_or_none(observation["temperature_2m_c"]) if observation else None,
                "weather_model_run_utc": _iso(row["weather_model_run_utc"]), "weather_available_at_utc": _iso(row["weather_available_at_utc"]), "observed_ingested_at_utc": observation["observed_at_utc"] if observation else None,
            })
    return records, {}


def _weather_summary(records: list[dict[str, Any]]) -> dict[str, Any]:
    if not records:
        return {"weather_model": "ecmwf_ifs", "weather_model_run_utc": None, "weather_available_at_utc": None, "city_hour_count": 0, "observed_weather_city_hour_count": 0, "cities": []}
    cities = []
    for city in sorted({row["city"] for row in records}):
        by_city = [row for row in records if row["city"] == city]
        temperature = [row["issued_temperature_2m_c"] for row in by_city if row["issued_temperature_2m_c"] is not None]
        humidity = [row["relative_humidity_pct"] for row in by_city if row["relative_humidity_pct"] is not None]
        wind = [row["wind_speed_10m_kmh"] for row in by_city if row["wind_speed_10m_kmh"] is not None]
        if not temperature or not humidity or not wind:
            raise ValueError(f"Incomplete issued weather summary for {city}")
        cities.append({"city": city, "temperature_2m_c": {"min": min(temperature), "max": max(temperature)}, "relative_humidity_pct": {"min": min(humidity), "max": max(humidity)}, "wind_speed_10m_kmh": {"min": min(wind), "max": max(wind)}})
    return {"weather_model": "ecmwf_ifs", "weather_model_run_utc": records[0]["weather_model_run_utc"], "weather_available_at_utc": records[0]["weather_available_at_utc"], "city_hour_count": len(records), "observed_weather_city_hour_count": sum(row["observed_temperature_2m_c"] is not None for row in records), "cities": cities}


def publish_dashboard_evaluation(*, spark: Any, forecast_output: str, input_root: str, gold_output: str, weather_gold: str, dashboard_output: str) -> dict[str, Any]:
    """Build and atomically publish prospective dashboard JSON from trusted lake data."""
    bucket, _ = _s3_parts(forecast_output)
    dashboard_bucket, dashboard_prefix = _s3_parts(dashboard_output)
    if bucket != dashboard_bucket:
        raise ValueError("Forecast and dashboard products must remain in the canonical data lake")
    client = _s3_client()
    outputs = _candidate_outputs(forecast_output, client)
    if not outputs:
        raise ValueError("No complete, unambiguous three-model prospective forecast issues are available")
    actuals = _gold_actuals(gold_output, client)
    curves, runtime = _forecast_rows(bucket, outputs, actuals, client)
    valid: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for issue, models in curves.items():
        if set(models) != set(_MODELS):
            continue
        valid[issue] = {model: _validate_curve(issue, model, models[model]) for model in _MODELS}
    if not valid:
        raise ValueError("No prospective issue passed the 24-hour three-model contract")
    latest_issue = max(valid)
    weather_by_issue, _ = _weather_records(input_root, weather_gold, set(valid), client)
    model_lineages: dict[str, dict[str, Any]] = {}
    for output in outputs:
        if output["issue"] == latest_issue:
            model_lineages[output["model"]] = _read_json(bucket, output["lineage_key"], client)
    completed = [lineage["created_at_utc"] for lineage in model_lineages.values()]
    published_at = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
    latest_models = []
    for model in _MODELS:
        rows = valid[latest_issue][model]
        source_lineage = model_lineages[model]
        latest_models.append({
            "model_id": _MODEL_IDS[model], "model_label": _MODEL_LABELS[model], "variant": "raw", "predictions": rows, "metrics": _metrics(rows),
            "provenance": runtime[f"{latest_issue}/{model}"] | {
                "prediction_sha256": source_lineage["prediction_sha256"],
                "input_bundle_manifest_sha256": source_lineage["input_bundle_manifest_sha256"],
                "model_artifact": source_lineage["artifact"],
            },
        })
    targets = [row["target_timestamp_utc"] for row in valid[latest_issue][_MODELS[0]]]
    actual_hours = [target for target in targets if actuals.get(target) is not None]
    prospective = {
        "schema_version": "1.0", "status": "prospective_forecast_retrospective_evaluation", "issue_id": latest_issue,
        "forecast_generation_type": "prospective_live_inference", "not_current_live_forecast": False,
        "forecast_origin_utc": valid[latest_issue][_MODELS[0]][0]["forecast_origin_utc"], "models": latest_models,
        "actual_coverage": {"available_target_hours": len(actual_hours), "total_target_hours": 24, "pending_target_hours": [target for target in targets if target not in actual_hours], "latest_available_target_utc": max(actual_hours) if actual_hours else None},
        "execution": {"model_execution_completed_at_utc": max(completed) if completed else None, "dashboard_published_at_utc": published_at},
        "provenance": {"source_artifacts": [f"s3://{bucket}/forecasts/v1/live/issue={latest_issue}/", gold_output], "dashboard_published_at_utc": published_at},
    }
    prospective["weather"] = _weather_summary(weather_by_issue.get(latest_issue, []))
    rolling_metrics = {}
    now = datetime.now(timezone.utc)
    for days in (7, 14):
        cutoff = now.timestamp() - days * 86400
        eligible = [issue for issue, models in valid.items() if datetime.fromisoformat(models[_MODELS[0]][0]["forecast_origin_utc"].replace("Z", "+00:00")).timestamp() >= cutoff]
        rolling_metrics[f"{days}_day"] = {model: _metrics([row for issue in eligible for row in valid[issue][model]]) for model in _MODELS}
    rolling = {"schema_version": "1.0", "status": "prospective_rolling_evaluation", "historical_not_live": False, "evaluation_scope": "genuine_prospective_performance", "generated_at_utc": published_at, "metrics": rolling_metrics, "issues": sorted(valid), "curves": valid, "weather": {"records": [row for issue in sorted(valid) for row in weather_by_issue.get(issue, [])]}}
    immutable_prefix = f"{dashboard_prefix}/evaluations/prospective/refresh={published_at.replace(':', '').replace('-', '')}"
    prospective_hash = _put_json(dashboard_bucket, f"{immutable_prefix}/prospective-forecast.json", prospective, client, immutable=True)
    rolling_hash = _put_json(dashboard_bucket, f"{immutable_prefix}/rolling-prospective-evaluation.json", rolling, client, immutable=True)
    _put_json(dashboard_bucket, f"{dashboard_prefix}/data/prospective-forecast.json", prospective, client)
    _put_json(dashboard_bucket, f"{dashboard_prefix}/data/rolling-prospective-evaluation.json", rolling, client)
    manifest = {"schema_version": "1.0", "status": "complete", "published_at_utc": published_at, "latest_issue_id": latest_issue, "objects": {"prospective-forecast.json": prospective_hash, "rolling-prospective-evaluation.json": rolling_hash}}
    manifest_hash = _put_json(dashboard_bucket, f"{dashboard_prefix}/data/prospective-evaluation-manifest.json", manifest, client)
    return {"latest_issue_id": latest_issue, "issue_count": len(valid), "publication_manifest_sha256": manifest_hash, "published_at_utc": published_at}
