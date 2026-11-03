"""Local Spark/Delta integration tests for the optional Silver adapter."""

from __future__ import annotations

from datetime import datetime, timezone
import importlib.util
import json
import os
from pathlib import Path
import shutil

import pytest

from texas_energy.data.silver import SilverValidationError
from texas_energy.ingestion.bronze import BronzeSourceTimeRange, bronze_envelope
from texas_energy.ingestion.raw import RawCapture, canonical_json_bytes
from texas_energy.spark import silver


def _capture(
    *,
    source: str,
    dataset: str,
    payload: dict[str, object],
    parameters: dict[str, str],
    retrieved_at: datetime,
) -> RawCapture:
    return RawCapture(
        source=source,
        dataset=dataset,
        endpoint="https://example.invalid/data",
        request_parameters=parameters,
        raw_payload=canonical_json_bytes(payload),
        retrieved_at=retrieved_at,
        status_code=200,
        response_headers={"Content-Type": "application/json"},
    )


def _eia_envelope(*, value: str, retrieved_at: datetime) -> dict[str, object]:
    periods = ("2024-01-01T00", "2024-01-01T01")
    payload = {
        "response": {
            "data": [
                {"period": period, "respondent": "ERCO", "type": "D", "value": value, "value-units": "megawatthours"}
                for period in periods
            ]
        }
    }
    return bronze_envelope(
        _capture(
            source="eia",
            dataset="electricity/rto/region-data",
            payload=payload,
            parameters={"respondent": "ERCO", "type": "D"},
            retrieved_at=retrieved_at,
        ),
        source_time_range=BronzeSourceTimeRange(
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
        ),
        units={"value": "megawatthours"},
    )


def _weather_envelope(dataset: str) -> dict[str, object]:
    payload = {
        "latitude": 32.794376,
        "longitude": -96.8215,
        "hourly": {
            "time": [1_704_067_200, 1_704_070_800],
            "temperature_2m": [10.0, 11.0],
            "relative_humidity_2m": [50, 51],
            "wind_speed_10m": [12.0, 13.0],
        }
    }
    return bronze_envelope(
        _capture(
            source="open_meteo",
            dataset=dataset,
            payload=payload,
            parameters={"latitude": "32.7767", "longitude": "-96.7970"},
            retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc),
        ),
        source_time_range=BronzeSourceTimeRange(
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
        ),
        units={"temperature_2m": "°C", "relative_humidity_2m": "%", "wind_speed_10m": "km/h"},
    )


def _write_envelope(path: Path, name: str, envelope: dict[str, object]) -> None:
    path.mkdir(parents=True, exist_ok=True)
    (path / name).write_text(json.dumps(envelope), encoding="utf-8")


def _invalid_weather_envelope() -> dict[str, object]:
    envelope = _weather_envelope("historical-weather")
    envelope["capture"]["request_parameters"]["latitude"] = "0"  # type: ignore[index]
    return envelope


def _configure_local_spark_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Avoid stale global Spark 3/Hadoop variables during a local PySpark 4 test."""

    java = shutil.which("java")
    if java is None:
        pytest.skip("Java is required for the optional local Spark/Delta integration test.")
    monkeypatch.setenv("JAVA_HOME", str(Path(java).resolve().parent.parent))
    monkeypatch.delenv("SPARK_HOME", raising=False)
    if os.name == "nt":
        hadoop_home = os.environ.get("HADOOP_HOME")
        if not hadoop_home or not (Path(hadoop_home) / "bin" / "winutils.exe").is_file():
            pytest.skip("Windows local Delta integration requires HADOOP_HOME\\bin\\winutils.exe.")


def test_spark_runtime_reports_a_clear_installation_error_when_optional_dependencies_are_absent() -> None:
    if importlib.util.find_spec("pyspark") and importlib.util.find_spec("delta"):
        pytest.skip("Optional Spark dependencies are installed; the real integration test covers this runtime.")
    with pytest.raises(silver.SparkDependencyError, match="PySpark and delta-spark"):
        silver._spark_runtime()


def test_malformed_bronze_is_rejected_before_any_silver_table_can_be_updated(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    bronze_input = tmp_path / "bronze"
    _write_envelope(bronze_input, "invalid-weather.json", _invalid_weather_envelope())
    monkeypatch.setattr(silver, "_spark_runtime", lambda: pytest.fail("Spark must not start for malformed Bronze input."))

    with pytest.raises(SilverValidationError, match="not one configured"):
        silver.run_local_silver_job(bronze_input=bronze_input, silver_output=tmp_path / "silver")


def test_local_delta_pipeline_writes_reruns_and_replaces_newer_revisions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pytest.importorskip("pyspark")
    pytest.importorskip("delta")
    _configure_local_spark_environment(monkeypatch)
    bronze_input = tmp_path / "bronze"
    silver_output = tmp_path / "silver"
    _write_envelope(bronze_input, "eia-original.json", _eia_envelope(value="45000", retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc)))
    _write_envelope(bronze_input, "weather-historical.json", _weather_envelope("historical-weather"))
    _write_envelope(bronze_input, "weather-forecast.json", _weather_envelope("forecast"))

    assert silver.run_local_silver_job(bronze_input=bronze_input, silver_output=silver_output) == {
        "demand": 2,
        "historical_weather": 2,
        "forecast_weather": 2,
    }
    spark, delta_table, _ = silver._spark_runtime()
    try:
        history_count_before_rerun = delta_table.forPath(
            spark, (silver_output / "demand").resolve().as_uri()
        ).history().count()
    finally:
        spark.stop()

    assert silver.run_local_silver_job(bronze_input=bronze_input, silver_output=silver_output) == {
        "demand": 2,
        "historical_weather": 2,
        "forecast_weather": 2,
    }

    spark, delta_table, _ = silver._spark_runtime()
    try:
        demand = spark.read.format("delta").load((silver_output / "demand").resolve().as_uri())
        historical = spark.read.format("delta").load((silver_output / "weather_historical").resolve().as_uri())
        forecast = spark.read.format("delta").load((silver_output / "weather_forecast").resolve().as_uri())
        assert demand.count() == 2
        assert demand.select("source", "dataset", "period_utc").distinct().count() == 2
        assert {field.name for field in demand.schema.fields} >= {
            "demand_value",
            "demand_unit",
            "respondent",
            "demand_type",
            "quality_flags",
            "period_utc",
        }
        assert {
            (row.respondent, row.demand_type, str(row.demand_value), row.demand_unit)
            for row in demand.select("respondent", "demand_type", "demand_value", "demand_unit").collect()
        } == {("ERCO", "D", "45000.000000000000", "megawatthours")}
        history = delta_table.forPath(spark, (silver_output / "demand").resolve().as_uri()).history()
        assert history.count() >= history_count_before_rerun
        latest = history.orderBy("version", ascending=False).first()
        if latest.operation == "MERGE":
            assert latest.operationMetrics.get("numTargetRowsUpdated", "0") == "0"
        assert historical.count() == 2
        assert forecast.count() == 2
        assert historical.select("city", "dataset", "observed_at_utc").distinct().count() == 2
        assert forecast.select("city", "dataset", "observed_at_utc").distinct().count() == 2
        assert {field.name for field in historical.schema.fields} >= {
            "requested_latitude",
            "provider_latitude",
            "provider_coordinate_distance_km",
        }
        assert {row.dataset for row in historical.select("dataset").distinct().collect()} == {"historical-weather"}
        assert {row.dataset for row in forecast.select("dataset").distinct().collect()} == {"forecast"}
    finally:
        spark.stop()

    _write_envelope(bronze_input, "eia-revised.json", _eia_envelope(value="46000", retrieved_at=datetime(2024, 1, 3, tzinfo=timezone.utc)))
    assert silver.run_local_silver_job(bronze_input=bronze_input, silver_output=silver_output)["demand"] == 2

    spark, _, _ = silver._spark_runtime()
    try:
        revised = spark.read.format("delta").load((silver_output / "demand").resolve().as_uri())
        assert revised.count() == 2
        assert {str(row.demand_value) for row in revised.select("demand_value").collect()} == {"46000.000000000000"}
    finally:
        spark.stop()


def test_existing_delta_schema_requires_explicit_rebuild_before_any_merge(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    pytest.importorskip("pyspark")
    pytest.importorskip("delta")
    _configure_local_spark_environment(monkeypatch)
    bronze_input = tmp_path / "bronze"
    silver_output = tmp_path / "silver"
    envelope = _eia_envelope(value="45000", retrieved_at=datetime(2024, 1, 2, tzinfo=timezone.utc))
    _write_envelope(bronze_input, "eia.json", envelope)

    spark, _, schemas = silver._spark_runtime()
    try:
        from texas_energy.data.silver import BronzeEnvelopeInput, transform_bronze_envelopes

        rows = transform_bronze_envelopes((BronzeEnvelopeInput("file:///bronze/eia.json", envelope),)).as_rows()["demand"]
        (
            spark.createDataFrame(rows, schema=schemas["demand"])
            .drop("respondent", "demand_type")
            .write.format("delta")
            .mode("errorifexists")
            .save((silver_output / "demand").resolve().as_uri())
        )
    finally:
        spark.stop()

    with pytest.raises(silver.SilverSchemaMigrationRequired, match="Rebuild to a new output directory"):
        silver.run_local_silver_job(bronze_input=bronze_input, silver_output=silver_output)

    spark, _, _ = silver._spark_runtime()
    try:
        existing = spark.read.format("delta").load((silver_output / "demand").resolve().as_uri())
        assert existing.count() == 2
        assert "respondent" not in existing.columns
        assert "demand_type" not in existing.columns
    finally:
        spark.stop()
