"""Build a bounded, immutable prospective input bundle from canonical Delta.

This runs inside the existing production Glue job after Silver and core-demand
Gold have committed.  Demand is read from authoritative Gold v2; issued IFS
weather remains in its separate Silver product because observed weather is not a
prospective model input.  The job deliberately collects only the 2,048 delayed
demand hours and 120 issued-weather rows needed by one forecast issue.  The
manifest is written last, making it the publication marker.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse


_CITIES = ("austin", "dallas", "el_paso", "houston", "san_antonio")
_CONTEXT_HOURS = 2048
_ISSUE_ID = re.compile(r"^\d{8}T\d{6}Z$")
_BUNDLE_GENERATION_TYPES = frozenset({"aws_managed_prospective_input", "aws_managed_historical_replay_input"})
_GOLD_CONTEXT_COLUMNS = (
    "demand_lag_1h_mwh",
    "demand_lag_24h_mwh",
    "demand_lag_168h_mwh",
    "demand_trailing_mean_24h_mwh",
    "demand_trailing_mean_168h_mwh",
)
_WEATHER_VALUE_COLUMNS = (
    "temperature_2m_c",
    "relative_humidity_pct",
    "apparent_temperature_c",
    "wind_speed_10m_kmh",
)
_WEATHER_UNITS = {
    "temperature_2m_unit": "°C",
    "relative_humidity_unit": "%",
    "apparent_temperature_unit": "°C",
    "wind_speed_10m_unit": "km/h",
}


class ForecastInputError(RuntimeError):
    """Silver cannot prove the causal contract for a requested issue."""


def _utc_hour(value: str, name: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except (TypeError, ValueError):
        raise ForecastInputError(f"{name} must be an ISO-8601 UTC timestamp.") from None
    if parsed.minute or parsed.second or parsed.microsecond:
        raise ForecastInputError(f"{name} must be an exact UTC hour.")
    return parsed


def _s3_prefix(uri: str) -> tuple[str, str]:
    parsed = urlparse(uri)
    if parsed.scheme != "s3" or not parsed.netloc or not parsed.path.strip("/"):
        raise ForecastInputError("forecast input output must be a non-root S3 prefix URI.")
    return parsed.netloc, parsed.path.strip("/")


def _validate_bundle_generation_type(generation_type: str, *, output_prefix: str) -> None:
    """Keep replay inputs physically and semantically separate from live issues."""

    if generation_type not in _BUNDLE_GENERATION_TYPES:
        raise ForecastInputError("forecast input generation type is unsupported.")
    _, prefix = _s3_prefix(output_prefix)
    is_historical_prefix = prefix == "forecast-inputs/v1/historical-replay" or prefix.startswith("forecast-inputs/v1/historical-replay/")
    if generation_type == "aws_managed_historical_replay_input" and not is_historical_prefix:
        raise ForecastInputError("Historical replay inputs must use the historical-replay prefix.")
    if generation_type == "aws_managed_prospective_input" and is_historical_prefix:
        raise ForecastInputError("Prospective inputs cannot use the historical-replay prefix.")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _as_utc(value: datetime) -> datetime:
    """Normalize Spark's timestamp materialization to an aware UTC value."""
    return value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value.astimezone(timezone.utc)


def _require_issuable_origin(issue: datetime, *, now: datetime | None = None) -> None:
    """Reject a future-labelled bundle before any immutable publication occurs."""

    observed_now = _as_utc(now or datetime.now(timezone.utc))
    if issue > observed_now:
        raise ForecastInputError("forecast_origin_utc cannot be later than the actual bundle issuance time.")


def _put_object_if_absent(client: object, **kwargs: object) -> None:
    """Issue one S3 PutObject with a signed ``If-None-Match: *`` header.

    The Glue 5 bundled botocore service model does not expose ``IfNoneMatch``
    as a Python ``put_object`` parameter even though S3 supports the HTTP
    conditional header.  Adding it at the ``before-call`` event keeps the
    generated request valid and signed, while preserving immutable publication
    under a single forecast-issue key.
    """

    from botocore.exceptions import ClientError

    events = client.meta.events  # type: ignore[attr-defined]
    event_name = "before-call.s3.PutObject"
    handler_id = f"texas-energy-if-none-match-{id(kwargs)}"

    def add_if_none_match(*, params: dict[str, object], **_: object) -> None:
        headers = params.setdefault("headers", {})
        if not isinstance(headers, dict):  # defensive: botocore always supplies a mapping
            raise ForecastInputError("S3 request headers cannot enforce immutable publication.")
        headers["If-None-Match"] = "*"

    events.register_first(event_name, add_if_none_match, unique_id=handler_id)
    try:
        client.put_object(**kwargs)  # type: ignore[attr-defined]
    except ClientError as error:
        code = str(error.response.get("Error", {}).get("Code", ""))
        if code in {"PreconditionFailed", "ConditionalRequestConflict"}:
            raise ForecastInputError("A forecast input object already exists; refusing overwrite.") from error
        raise
    finally:
        events.unregister(event_name, unique_id=handler_id)


def _row_value(row: object, field: str) -> object:
    """Read one field from a Spark Row or a mapping used by focused tests."""

    try:
        return row[field]  # type: ignore[index]
    except (KeyError, TypeError, IndexError):
        raise ForecastInputError(f"Input row lacks required field: {field}.") from None


def _validate_gold_context_rows(
    rows: list[object], *, context_start: datetime, context_end: datetime,
) -> dict[str, str]:
    """Prove Gold v2's persisted causal features match its selected history."""

    if len(rows) != _CONTEXT_HOURS:
        raise ForecastInputError("Gold v2 does not contain the required 2,048-hour delayed context.")
    actual_times = [_as_utc(_row_value(row, "period_utc")) for row in rows]
    expected_times = [context_start + timedelta(hours=index) for index in range(_CONTEXT_HOURS)]
    if actual_times != expected_times:
        raise ForecastInputError("Gold v2 demand history is not contiguous through the allowed context end.")
    values: dict[datetime, float] = {}
    versions: set[str] = set()
    for row, timestamp in zip(rows, actual_times, strict=True):
        if _row_value(row, "respondent") != "ERCO" or _row_value(row, "demand_type") != "D":
            raise ForecastInputError("Gold v2 demand context is not the canonical ERCO/D contract.")
        if _row_value(row, "demand_unit") != "megawatthours":
            raise ForecastInputError("Gold v2 demand context is not hourly megawatthours.")
        if _row_value(row, "weather_availability") != "not_required_for_core_demand":
            raise ForecastInputError("Gold v2 demand context has an incompatible weather lineage contract.")
        version = _row_value(row, "gold_contract_version")
        if not isinstance(version, str) or not version:
            raise ForecastInputError("Gold v2 demand context lacks its contract version.")
        versions.add(version)
        value = float(_row_value(row, "observed_average_demand_mw"))
        if not math.isfinite(value):
            raise ForecastInputError("Gold v2 demand context contains a non-finite demand value.")
        values[timestamp] = value
    if len(versions) != 1:
        raise ForecastInputError("Gold v2 demand context mixes contract versions.")

    context_row = rows[-1]
    expected_features = {
        "demand_lag_1h_mwh": values[context_end - timedelta(hours=1)],
        "demand_lag_24h_mwh": values[context_end - timedelta(hours=24)],
        "demand_lag_168h_mwh": values[context_end - timedelta(hours=168)],
        "demand_trailing_mean_24h_mwh": sum(
            values[context_end - timedelta(hours=hour)] for hour in range(1, 25)
        ) / 24,
        "demand_trailing_mean_168h_mwh": sum(
            values[context_end - timedelta(hours=hour)] for hour in range(1, 169)
        ) / 168,
    }
    for field, expected in expected_features.items():
        actual = float(_row_value(context_row, field))
        if not math.isfinite(actual) or not math.isclose(actual, expected, rel_tol=0.0, abs_tol=1e-6):
            raise ForecastInputError(f"Gold v2 persisted feature does not match its causal context: {field}.")
    return {"gold_contract_version": versions.pop()}


def _validate_issued_weather_rows(
    rows: list[object], *, issue: datetime, run: datetime, target_start: datetime,
) -> dict[str, object]:
    """Validate the full five-city IFS contract before manifest-last publication."""

    if len(rows) != len(_CITIES) * 24:
        raise ForecastInputError("Issued IFS weather does not cover five cities and all 24 target hours.")
    expected_keys = {(city, target_start + timedelta(hours=hour)) for city in _CITIES for hour in range(24)}
    actual_keys: set[tuple[str, datetime]] = set()
    availability_bases: set[str] = set()
    retrieval_times: list[datetime] = []
    for row in rows:
        city = _row_value(row, "city")
        valid_time = _as_utc(_row_value(row, "valid_time_utc"))
        if not isinstance(city, str):
            raise ForecastInputError("Issued IFS weather has an invalid city identifier.")
        actual_keys.add((city, valid_time))
        if _as_utc(_row_value(row, "weather_model_run_utc")) != run:
            raise ForecastInputError("Issued IFS weather mixes model-run timestamps.")
        if _as_utc(_row_value(row, "weather_available_at_utc")) > issue:
            raise ForecastInputError("Issued IFS weather was not available by forecast issuance.")
        retrieval_times.append(_as_utc(_row_value(row, "retrieval_time_utc")))
        basis = _row_value(row, "availability_basis")
        if not isinstance(basis, str) or not basis:
            raise ForecastInputError("Issued IFS weather lacks an availability basis.")
        availability_bases.add(basis)
        for field in _WEATHER_VALUE_COLUMNS:
            if not math.isfinite(float(_row_value(row, field))):
                raise ForecastInputError(f"Issued IFS weather contains a non-finite value: {field}.")
        for field, unit in _WEATHER_UNITS.items():
            if _row_value(row, field) != unit:
                raise ForecastInputError(f"Issued IFS weather has an incompatible unit: {field}.")
    if actual_keys != expected_keys:
        raise ForecastInputError("Issued IFS weather target timestamps or city coverage are incomplete.")
    return {
        "weather_availability_bases": sorted(availability_bases),
        "weather_retrieval_first_utc": min(retrieval_times).isoformat().replace("+00:00", "Z"),
        "weather_retrieval_last_utc": max(retrieval_times).isoformat().replace("+00:00", "Z"),
    }


def write_live_input_bundle(
    *, spark: object, gold_demand_features: str, silver_issued_weather: str, output_prefix: str,
    forecast_origin_utc: str, weather_model_run_utc: str, forecast_issue_id: str,
    generation_type: str = "aws_managed_prospective_input",
) -> dict[str, object]:
    """Create one manifest-last bundle from committed Gold and Silver products.

    The caller supplies the issue and IFS initialization explicitly.  This avoids
    silently choosing a newer forecast run and gives the Step Functions issue a
    stable idempotency key.  Existing objects are never overwritten.
    """
    from pyspark.sql import functions as F

    issue = _utc_hour(forecast_origin_utc, "forecast_origin_utc")
    _require_issuable_origin(issue)
    _validate_bundle_generation_type(generation_type, output_prefix=output_prefix)
    expected_issue_id = issue.strftime("%Y%m%dT%H%M%SZ")
    if not _ISSUE_ID.fullmatch(forecast_issue_id) or forecast_issue_id != expected_issue_id:
        raise ForecastInputError("forecast_issue_id must exactly match forecast_origin_utc in YYYYMMDDTHHMMSSZ form.")
    run = _utc_hour(weather_model_run_utc, "weather_model_run_utc")
    context_end = issue - timedelta(hours=24)
    context_start = context_end - timedelta(hours=_CONTEXT_HOURS - 1)
    target_start, target_end = issue + timedelta(hours=1), issue + timedelta(hours=24)

    demand = (
        spark.read.format("delta").load(gold_demand_features)
        .where((F.col("period_utc") >= F.lit(context_start)) & (F.col("period_utc") <= F.lit(context_end)))
        .where((F.col("respondent") == "ERCO") & (F.col("demand_type") == "D") & (F.col("demand_unit") == "megawatthours"))
        .select(
            "period_utc", F.col("observed_average_demand_mw").cast("double").alias("observed_average_demand_mw"),
            "respondent", "demand_type", "demand_unit", "gold_contract_version", "weather_availability",
            "demand_bronze_uri", "demand_source_raw_payload_sha256",
            *[F.col(column).cast("double").alias(column) for column in _GOLD_CONTEXT_COLUMNS],
        )
        .orderBy("period_utc")
    )
    gold_rows = demand.collect()
    gold_contract = _validate_gold_context_rows(
        gold_rows, context_start=context_start, context_end=context_end,
    )
    demand = demand.withColumn("source_unit", F.lit("megawatthours")).drop("demand_unit", "gold_contract_version", "weather_availability", "demand_bronze_uri", "demand_source_raw_payload_sha256", *_GOLD_CONTEXT_COLUMNS)
    # Serialize the normalized contract rather than the Gold-validation rows.
    demand_rows = demand.collect()

    weather = (
        spark.read.format("delta").load(silver_issued_weather)
        .where(F.col("weather_model") == "ecmwf_ifs")
        .where(F.col("weather_model_run_utc") == F.lit(run))
        .where((F.col("valid_time_utc") >= F.lit(target_start)) & (F.col("valid_time_utc") <= F.lit(target_end)))
        .where(F.col("weather_available_at_utc") <= F.lit(issue))
        .select(
            "city", "valid_time_utc", "weather_model_run_utc", "weather_available_at_utc", "retrieval_time_utc", "availability_basis",
            F.col("temperature_2m").alias("temperature_2m_c"),
            F.col("relative_humidity_2m").alias("relative_humidity_pct"),
            F.col("apparent_temperature").alias("apparent_temperature_c"),
            F.col("wind_speed_10m").alias("wind_speed_10m_kmh"),
            F.col("temperature_unit").alias("temperature_2m_unit"),
            F.col("relative_humidity_unit").alias("relative_humidity_unit"),
            F.col("apparent_temperature_unit").alias("apparent_temperature_unit"),
            F.col("wind_speed_unit").alias("wind_speed_10m_unit"),
        ).orderBy("city", "valid_time_utc")
    )
    weather_rows = weather.collect()
    weather_contract = _validate_issued_weather_rows(
        weather_rows, issue=issue, run=run, target_start=target_start,
    )

    bucket, prefix = _s3_prefix(output_prefix)
    issue_prefix = f"{prefix.rstrip('/')}/issue={forecast_issue_id}"
    keys = {
        "demand": f"{issue_prefix}/demand_history.parquet",
        "weather": f"{issue_prefix}/weather_forecasts.parquet",
        "manifest": f"{issue_prefix}/input_manifest.json",
    }
    import boto3
    client = boto3.client("s3")
    existing = client.list_objects_v2(Bucket=bucket, Prefix=f"{issue_prefix}/", MaxKeys=1)
    if existing.get("KeyCount", 0):
        raise ForecastInputError("A forecast input bundle already exists for this issue; refusing overwrite.")

    # Glue 5 contains pandas/pyarrow.  This bounded conversion writes the exact
    # single-file input names expected by the existing SageMaker runtime.
    import pandas as pd
    with tempfile.TemporaryDirectory(prefix="texas-energy-input-") as temporary:
        temp = Path(temporary)
        demand_path, weather_path = temp / "demand_history.parquet", temp / "weather_forecasts.parquet"
        pd.DataFrame([row.asDict(recursive=True) for row in demand_rows]).to_parquet(demand_path, index=False)
        pd.DataFrame([row.asDict(recursive=True) for row in weather_rows]).to_parquet(weather_path, index=False)
        demand_sha, weather_sha = _sha256(demand_path), _sha256(weather_path)
        manifest = {
            "schema_version": "1.0", "generation_type": generation_type,
            "forecast_origin_utc": issue.isoformat().replace("+00:00", "Z"),
            "context_end_utc": context_end.isoformat().replace("+00:00", "Z"),
            "demand_availability_lag_hours": 24, "demand_context_hours": _CONTEXT_HOURS,
            "weather_model": "ecmwf_ifs", "weather_model_run_utc": run.isoformat().replace("+00:00", "Z"),
            "weather_available_at_or_before_issue": True, "demand_sha256": demand_sha, "weather_sha256": weather_sha,
            "gold_demand_features_uri": gold_demand_features,
            "gold_contract_version": gold_contract["gold_contract_version"],
            "gold_context_feature_validation": "persisted_features_match_selected_2048_hour_context",
            "silver_issued_weather_uri": silver_issued_weather,
            **weather_contract,
        }
        manifest_path = temp / "input_manifest.json"
        manifest_path.write_text(json.dumps(manifest, sort_keys=True, indent=2) + "\n", encoding="utf-8")
        for local, key in ((demand_path, keys["demand"]), (weather_path, keys["weather"]), (manifest_path, keys["manifest"])):
            with local.open("rb") as body:
                _put_object_if_absent(
                    client,
                    Bucket=bucket,
                    Key=key,
                    Body=body,
                    ServerSideEncryption="AES256",
                )
    return {"input_bundle_uri": f"s3://{bucket}/{issue_prefix}/", "forecast_origin_utc": manifest["forecast_origin_utc"], "context_end_utc": manifest["context_end_utc"], "demand_rows": len(demand_rows), "weather_rows": len(weather_rows), "demand_sha256": demand_sha, "weather_sha256": weather_sha, "gold_demand_features_uri": gold_demand_features, "gold_contract_version": gold_contract["gold_contract_version"]}
