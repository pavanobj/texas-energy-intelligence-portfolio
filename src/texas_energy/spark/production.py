"""Distributed canonical-Bronze to Silver/Gold execution for AWS Glue.

The smoke runner intentionally collects its small fixture at the driver.  This
module is the production counterpart: Spark reads immutable Bronze envelopes in
partitions, validates each envelope with the same typed contract, then performs
deterministic global deduplication before Delta merge writes.
"""

from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any, Iterable

from texas_energy.data.gold import GoldFeatureConfig
from texas_energy.data.silver import BronzeEnvelopeInput, validated_bronze_records
from texas_energy.spark.gold import run_core_demand_gold_job
from texas_energy.spark.forecast_input import write_live_input_bundle
from texas_energy.spark.silver import _child_location, _merge_frame, _spark_schemas


class ProductionBronzeValidationError(RuntimeError):
    """A canonical Bronze object cannot be safely transformed."""


def run_production_silver_gold_job(
    *,
    bronze_input: str,
    silver_output: str,
    gold_output: str,
    observed_weather_lineage_output: str | None = None,
    spark: Any,
    lag_hours: tuple[int, ...] = (1, 24, 168),
    trailing_window_hours: tuple[int, ...] = (24, 168),
    forecast_input_output: str | None = None,
    forecast_origin_utc: str | None = None,
    weather_model_run_utc: str | None = None,
    forecast_issue_id: str | None = None,
    forecast_generation_type: str = "aws_managed_prospective_input",
) -> dict[str, object]:
    """Transform canonical Bronze in parallel and write merge-safe Delta tables.

    No source response is collected to the driver.  Only the final row counts
    are returned, which keeps Glue/Step Functions execution state small and
    avoids putting raw provider payloads into orchestration history.
    """

    from pyspark.sql import Window, functions as F

    source = (
        spark.read.format("text")
        .option("wholetext", "true")
        .option("recursiveFileLookup", "true")
        .load(bronze_input)
        .selectExpr("input_file_name() AS bronze_uri", "value")
    )
    if source.limit(1).count() == 0:
        raise ProductionBronzeValidationError("Canonical Bronze input contains no envelope objects.")

    demand_rows, historical_rows, forecast_rows, issued_rows = _parse_bronze_partitions(source)
    schemas = _spark_schemas()
    demand = spark.createDataFrame(demand_rows, schemas["demand"])
    historical = spark.createDataFrame(historical_rows, schemas["weather"])
    forecast = spark.createDataFrame(forecast_rows, schemas["weather"])
    issued = spark.createDataFrame(issued_rows, schemas["issued_weather"])

    # Repeated immutable captures are expected.  Pick the latest captured
    # record deterministically without mixing historical and issued forecasts.
    demand = _deduplicate(demand, ("source", "dataset", "period_utc"))
    historical = _deduplicate(historical, ("city", "dataset", "observed_at_utc"))
    forecast = _deduplicate(
        forecast,
        ("city", "dataset", "observed_at_utc", "source_request_fingerprint"),
    )
    issued = _deduplicate(issued, ("city", "weather_model_run_utc", "valid_time_utc"))

    _merge_frame(
        spark, None, demand, _child_location(silver_output, "demand"),
        key_columns=("source", "dataset", "period_utc"), schema=schemas["demand"],
    )
    _merge_frame(
        spark, None, issued, _child_location(silver_output, "weather_issued_ifs"),
        key_columns=("city", "weather_model_run_utc", "valid_time_utc"), schema=schemas["issued_weather"],
    )
    _merge_frame(
        spark, None, historical, _child_location(silver_output, "weather_historical"),
        key_columns=("city", "dataset", "observed_at_utc"), schema=schemas["weather"],
    )
    _merge_frame(
        spark, None, forecast, _child_location(silver_output, "weather_forecast"),
        key_columns=("city", "dataset", "observed_at_utc"), schema=schemas["weather"],
    )
    # Core demand Gold does not depend on complete realized-weather coverage.
    # The strictly validated observed-weather Silver table remains available for
    # separate enrichment and analysis; it is never substituted for issued
    # forecast weather in model-facing prospective inputs.
    gold_rows = run_core_demand_gold_job(
        silver_input=silver_output,
        gold_output=gold_output,
        observed_weather_lineage_output=observed_weather_lineage_output,
        config=GoldFeatureConfig(lag_hours=lag_hours, trailing_window_hours=trailing_window_hours),
        spark=spark,
        delta_table=None,
    )
    result: dict[str, object] = {
        "silver_demand": demand.count(),
        "silver_historical_weather": historical.count(),
        "silver_forecast_weather": forecast.count(),
        "silver_issued_ifs_weather": issued.count(),
        "gold_features": gold_rows,
    }
    supplied = (forecast_input_output, forecast_origin_utc, weather_model_run_utc, forecast_issue_id)
    if any(value is not None for value in supplied):
        if not all(supplied):
            raise ProductionBronzeValidationError(
                "Forecast input publication requires output, issue time, and IFS model run together."
            )
        result["forecast_input"] = write_live_input_bundle(
            spark=spark,
            gold_demand_features=gold_output,
            silver_issued_weather=_child_location(silver_output, "weather_issued_ifs"),
            output_prefix=forecast_input_output,
            forecast_origin_utc=forecast_origin_utc,
            weather_model_run_utc=weather_model_run_utc,
            forecast_issue_id=forecast_issue_id,
            generation_type=forecast_generation_type,
        )
    return result


def _parse_bronze_partitions(frame: Any) -> tuple[Any, Any, Any, Any]:
    """Return distributed demand, observed/current/issued weather RDDs."""

    def parse(rows: Iterable[Any]) -> Iterable[tuple[str, dict[str, object]]]:
        for row in rows:
            try:
                decoded = json.loads(row.value)
                if not isinstance(decoded, dict):
                    raise ValueError("Bronze envelope is not a JSON object")
                records = validated_bronze_records(
                    (BronzeEnvelopeInput(uri=str(row.bronze_uri), envelope=decoded),)
                )
            except Exception as error:
                raise ProductionBronzeValidationError(
                    "Canonical Bronze validation failed in a Spark partition."
                ) from error
            for record in records.demand:
                yield "demand", _row(record)
            for record in records.historical_weather:
                yield "historical", _row(record)
            for record in records.forecast_weather:
                yield "forecast", _row(record)
            for record in records.issued_forecast_weather:
                yield "issued", _row(record)

    parsed = frame.rdd.mapPartitions(parse).persist()
    return (
        parsed.filter(lambda value: value[0] == "demand").values(),
        parsed.filter(lambda value: value[0] == "historical").values(),
        parsed.filter(lambda value: value[0] == "forecast").values(),
        parsed.filter(lambda value: value[0] == "issued").values(),
    )


def _row(record: Any) -> dict[str, object]:
    row = asdict(record)
    row["quality_flags"] = list(record.quality_flags)
    return row


def _deduplicate(frame: Any, keys: tuple[str, ...]) -> Any:
    from pyspark.sql import Window, functions as F

    window = Window.partitionBy(*keys).orderBy(
        F.col("ingested_at_utc").desc(),
        F.col("source_raw_payload_sha256").desc(),
        F.col("bronze_uri").desc(),
    )
    return frame.withColumn("_dedupe_rank", F.row_number().over(window)).where("_dedupe_rank = 1").drop("_dedupe_rank")
