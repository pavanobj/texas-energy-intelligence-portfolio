"""Optional local PySpark/Delta writer for validated Silver datasets."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
from typing import Any

from texas_energy.data.silver import (
    BronzeEnvelopeInput,
    load_local_bronze_envelopes,
    load_verified_ercot_parquet,
    transform_bronze_envelopes,
)


class SparkDependencyError(RuntimeError):
    """PySpark/Delta Lake is unavailable from the selected local environment."""


class SilverSchemaMigrationRequired(RuntimeError):
    """An existing Delta table does not match the reviewed Silver schema."""


def run_local_silver_job(
    *,
    bronze_input: Path,
    silver_output: Path,
    verified_ercot_parquet: Path | None = None,
) -> dict[str, int]:
    """Run the reviewed local Spark/Delta implementation unchanged."""

    return run_silver_job(
        bronze_input=bronze_input,
        silver_output=silver_output,
        verified_ercot_parquet=verified_ercot_parquet,
    )


def run_silver_job(
    *,
    bronze_input: Path | str,
    silver_output: Path | str,
    verified_ercot_parquet: Path | None = None,
    spark: Any | None = None,
    delta_table: Any | None = None,
) -> dict[str, int]:
    """Read local Bronze JSON envelopes and merge deterministic Delta Silver tables.

    The default runtime remains the existing local PySpark/Delta environment.
    Passing an existing ``spark`` session and ``delta_table`` lets a Glue job use
    Glue's preinstalled Spark and Delta libraries without resolving local pip
    packages. URI inputs (for example ``s3://``) are passed to Spark unchanged.

    Remote Bronze input is intentionally bounded to fixture/smoke workloads: the
    current typed Bronze validator receives complete envelopes on the driver.
    It is not a replacement for a distributed production Bronze reader.
    """

    # Bronze envelopes remain immutable evidence; Silver normalizes them into
    # merge-safe Delta tables while retaining source and request provenance.

    # Validate local input before creating a JVM.  This keeps malformed Bronze
    # from starting a Spark application and preserves the established local
    # contract; remote S3 input remains Spark-distributed.
    local_datasets = None
    if not _is_uri(str(bronze_input)):
        local_datasets = transform_bronze_envelopes(load_local_bronze_envelopes(Path(bronze_input)))

    owns_spark = spark is None
    if spark is None:
        spark, delta_table, schemas = _spark_runtime()
    else:
        if delta_table is None:
            delta_table = _optional_delta_table()
        schemas = _spark_schemas()
    try:
        datasets = local_datasets or transform_bronze_envelopes(_load_bronze_envelopes(bronze_input, spark=spark))
        rows = datasets.as_rows()
        if verified_ercot_parquet is not None:
            rows["demand"] = [record.__dict__.copy() for record in load_verified_ercot_parquet(verified_ercot_parquet)]
            for row in rows["demand"]:
                row["quality_flags"] = list(row["quality_flags"])
        _merge_table(
            spark,
            delta_table,
            rows["demand"],
            _child_location(silver_output, "demand"),
            key_columns=("source", "dataset", "period_utc"),
            schema=schemas["demand"],
        )
        _merge_table(
            spark,
            delta_table,
            rows["historical_weather"],
            _child_location(silver_output, "weather_historical"),
            key_columns=("city", "dataset", "observed_at_utc"),
            schema=schemas["weather"],
        )
        _merge_table(
            spark,
            delta_table,
            rows["forecast_weather"],
            _child_location(silver_output, "weather_forecast"),
            key_columns=("city", "dataset", "observed_at_utc"),
            schema=schemas["weather"],
        )
        _merge_table(
            spark,
            delta_table,
            rows["issued_forecast_weather"],
            _child_location(silver_output, "weather_issued_ifs"),
            key_columns=("city", "weather_model_run_utc", "valid_time_utc"),
            schema=schemas["issued_weather"],
        )
        # Retain the established local return contract; the new, independent
        # table is empty for legacy fixtures and queried directly when present.
        return {name: len(rows[name]) for name in ("demand", "historical_weather", "forecast_weather")}
    finally:
        if owns_spark:
            spark.stop()


def _load_bronze_envelopes(path: Path | str, *, spark: Any) -> tuple[BronzeEnvelopeInput, ...]:
    """Load local Bronze files or complete envelope JSON files through Spark."""

    location = str(path)
    if not _is_uri(location):
        return load_local_bronze_envelopes(Path(location))
    try:
        rows = (
            spark.read.format("text")
            .option("wholetext", "true")
            .option("recursiveFileLookup", "true")
            .load(location)
            .selectExpr("input_file_name() AS uri", "value")
            .collect()
        )
    except Exception as error:
        raise SilverValidationError("Spark could not read the Bronze envelope URI.") from error
    if not rows:
        raise SilverValidationError("Bronze input URI contains no JSON envelope files.")
    envelopes: list[BronzeEnvelopeInput] = []
    for row in rows:
        try:
            envelope = json.loads(row.value)
        except (TypeError, json.JSONDecodeError):
            raise SilverValidationError("A Bronze envelope URI contains unreadable JSON.") from None
        if not isinstance(envelope, dict):
            raise SilverValidationError("A Bronze envelope must be a JSON object.")
        envelopes.append(BronzeEnvelopeInput(uri=str(row.uri), envelope=envelope))
    return tuple(envelopes)


def _is_uri(value: str) -> bool:
    return "://" in value


def _child_location(parent: Path | str, child: str) -> Path | str:
    value = str(parent)
    return f"{value.rstrip('/')}/{child}" if _is_uri(value) else Path(value) / child


def delta_location(value: Path | str) -> str:
    """Return a local file URI or preserve an S3-compatible Spark URI."""

    text = str(value)
    return text if _is_uri(text) else Path(text).expanduser().resolve().as_uri()


def _delta_table() -> Any:
    try:
        from delta import DeltaTable
    except ImportError:
        raise SparkDependencyError("Delta Lake is unavailable from the selected Spark runtime.") from None
    return DeltaTable


def _optional_delta_table() -> Any | None:
    """Return the pip Delta wrapper when present, otherwise use Glue's JVM API.

    Glue 5 ships Delta's Spark/JVM implementation but does not expose the
    separate ``delta-spark`` Python package.  Keeping ``None`` here selects
    the SQL merge path below without changing local Delta behavior.
    """

    try:
        return _delta_table()
    except SparkDependencyError:
        return None


def _is_delta_table(spark: Any, delta_table: Any | None, path_text: str) -> bool:
    if delta_table is not None:
        return bool(delta_table.isDeltaTable(spark, path_text))
    try:
        return bool(
            spark._jvm.io.delta.tables.DeltaTable.isDeltaTable(  # noqa: SLF001 - Glue's supported JVM Delta API.
                spark._jsparkSession,  # noqa: SLF001 - required by the Java overload.
                path_text,
            )
        )
    except Exception:
        return False


def _delta_columns(spark: Any, delta_table: Any | None, path_text: str) -> tuple[str, ...]:
    if delta_table is not None:
        return tuple(field.name for field in delta_table.forPath(spark, path_text).toDF().schema.fields)
    return tuple(field.name for field in spark.read.format("delta").load(path_text).schema.fields)


def _sql_merge(
    spark: Any,
    frame: Any,
    *,
    table_path: str,
    key_columns: tuple[str, ...],
    expected_columns: tuple[str, ...],
) -> None:
    """Execute a Delta MERGE through Spark SQL when Glue has no Python wrapper."""

    import uuid

    source_view = f"texas_energy_source_{uuid.uuid4().hex}"
    frame.createOrReplaceTempView(source_view)
    try:
        predicate = " AND ".join(f"target.`{column}` = source.`{column}`" for column in key_columns)
        changed = " OR ".join(
            f"NOT (target.`{column}` <=> source.`{column}`)"
            for column in expected_columns
            if column not in key_columns
        )
        columns = ", ".join(f"`{column}`" for column in expected_columns)
        source_columns = ", ".join(f"source.`{column}`" for column in expected_columns)
        quoted_path = table_path.replace("`", "``")
        spark.sql(
            f"""
            MERGE INTO delta.`{quoted_path}` AS target
            USING `{source_view}` AS source
            ON {predicate}
            WHEN MATCHED AND ({changed}) THEN UPDATE SET *
            WHEN NOT MATCHED THEN INSERT ({columns}) VALUES ({source_columns})
            """
        )
    finally:
        spark.catalog.dropTempView(source_view)


def _spark_schemas() -> dict[str, Any]:
    try:
        from pyspark.sql.types import ArrayType, DecimalType, DoubleType, IntegerType, StringType, StructField, StructType, TimestampType
    except ImportError:
        raise SparkDependencyError("PySpark is unavailable from the selected Spark runtime.") from None
    common = [
        StructField("source", StringType(), nullable=False),
        StructField("dataset", StringType(), nullable=False),
    ]
    provenance = [
        StructField("source_request_fingerprint", StringType(), nullable=False),
        StructField("source_raw_payload_sha256", StringType(), nullable=False),
        StructField("sanitized_payload_sha256", StringType(), nullable=False),
        StructField("ingested_at_utc", TimestampType(), nullable=False),
        StructField("bronze_uri", StringType(), nullable=False),
        StructField("quality_status", StringType(), nullable=False),
        StructField("quality_flags", ArrayType(StringType(), containsNull=False), nullable=False),
    ]
    return {
        "demand": StructType(
            common
            + [
                StructField("period_utc", TimestampType(), nullable=False),
                StructField("respondent", StringType(), nullable=False),
                StructField("demand_type", StringType(), nullable=False),
                StructField("demand_value", DecimalType(38, 12), nullable=False),
                StructField("demand_unit", StringType(), nullable=False),
            ]
            + provenance
        ),
        "weather": StructType(
            common
            + [
                StructField("city", StringType(), nullable=False),
                StructField("requested_latitude", DoubleType(), nullable=False),
                StructField("requested_longitude", DoubleType(), nullable=False),
                StructField("provider_latitude", DoubleType(), nullable=False),
                StructField("provider_longitude", DoubleType(), nullable=False),
                StructField("provider_coordinate_distance_km", DoubleType(), nullable=False),
                StructField("observed_at_utc", TimestampType(), nullable=False),
                StructField("temperature_2m", DoubleType(), nullable=False),
                StructField("relative_humidity_2m", DoubleType(), nullable=False),
                StructField("wind_speed_10m", DoubleType(), nullable=False),
                StructField("temperature_unit", StringType(), nullable=False),
                StructField("relative_humidity_unit", StringType(), nullable=False),
                StructField("wind_speed_unit", StringType(), nullable=False),
            ]
            + provenance
        ),
        "issued_weather": StructType(
            common
            + [
                StructField("city", StringType(), nullable=False),
                StructField("requested_latitude", DoubleType(), nullable=False),
                StructField("requested_longitude", DoubleType(), nullable=False),
                StructField("provider_latitude", DoubleType(), nullable=False),
                StructField("provider_longitude", DoubleType(), nullable=False),
                StructField("provider_coordinate_distance_km", DoubleType(), nullable=False),
                StructField("valid_time_utc", TimestampType(), nullable=False),
                StructField("weather_model", StringType(), nullable=False),
                StructField("weather_model_run_utc", TimestampType(), nullable=False),
                StructField("weather_available_at_utc", TimestampType(), nullable=False),
                StructField("retrieval_time_utc", TimestampType(), nullable=False),
                StructField("availability_basis", StringType(), nullable=False),
                StructField("forecast_lead_hours", IntegerType(), nullable=False),
                StructField("temperature_2m", DoubleType(), nullable=False),
                StructField("relative_humidity_2m", DoubleType(), nullable=False),
                StructField("apparent_temperature", DoubleType(), nullable=False),
                StructField("wind_speed_10m", DoubleType(), nullable=False),
                StructField("temperature_unit", StringType(), nullable=False),
                StructField("relative_humidity_unit", StringType(), nullable=False),
                StructField("apparent_temperature_unit", StringType(), nullable=False),
                StructField("wind_speed_unit", StringType(), nullable=False),
            ]
            + provenance
        ),
    }


def _ensure_local_parent(location: Path | str) -> None:
    if not _is_uri(str(location)):
        Path(location).parent.mkdir(parents=True, exist_ok=True)


def _spark_runtime() -> tuple[Any, Any, dict[str, Any]]:
    try:
        from delta import DeltaTable, configure_spark_with_delta_pip
        from pyspark.sql import SparkSession
    except ImportError:
        raise SparkDependencyError(
            "PySpark and delta-spark are required. Install the project's silver optional dependency group."
        ) from None
    # Spark launches Python workers through the environment. Default to the
    # interpreter running this local job so the worker cannot accidentally use
    # a system Python with incompatible PySpark dependencies.
    os.environ.setdefault("PYSPARK_PYTHON", sys.executable)
    builder = (
        SparkSession.builder.master("local[2]").appName("texas-energy-silver")
        .config("spark.sql.extensions", "io.delta.sql.DeltaSparkSessionExtension")
        .config("spark.sql.catalog.spark_catalog", "org.apache.spark.sql.delta.catalog.DeltaCatalog")
        .config("spark.sql.session.timeZone", "UTC")
        .config("spark.sql.shuffle.partitions", "4")
        .config("spark.pyspark.python", sys.executable)
    )
    return configure_spark_with_delta_pip(builder).getOrCreate(), DeltaTable, _spark_schemas()


def _merge_table(
    spark: Any,
    delta_table: Any,
    rows: list[dict[str, object]],
    table_path: Path | str,
    *,
    key_columns: tuple[str, ...],
    schema: Any,
) -> None:
    """Create or merge a table, using source business keys to prevent rerun duplicates."""

    if not rows:
        return
    _ensure_local_parent(table_path)
    path_text = delta_location(table_path)
    frame = spark.createDataFrame(rows, schema=schema)
    _merge_frame(
        spark,
        delta_table,
        frame,
        table_path,
        key_columns=key_columns,
        schema=schema,
    )


def _merge_frame(
    spark: Any,
    delta_table: Any,
    frame: Any,
    table_path: Path | str,
    *,
    key_columns: tuple[str, ...],
    schema: Any,
) -> None:
    """Create or merge an already-distributed Spark DataFrame into Delta."""

    _ensure_local_parent(table_path)
    path_text = delta_location(table_path)
    if not _is_delta_table(spark, delta_table, path_text):
        frame.write.format("delta").mode("errorifexists").save(path_text)
        return
    expected_columns = tuple(field.name for field in schema.fields)
    target_columns = _delta_columns(spark, delta_table, path_text)
    if target_columns != expected_columns:
        raise SilverSchemaMigrationRequired(
            "Existing Silver Delta schema does not match the reviewed contract. "
            "Rebuild to a new output directory from Bronze envelopes, validate it, then perform an approved cutover."
        )
    if delta_table is None:
        _sql_merge(
            spark,
            frame,
            table_path=path_text,
            key_columns=key_columns,
            expected_columns=expected_columns,
        )
        return
    target = delta_table.forPath(spark, path_text)
    predicate = " AND ".join(f"target.`{column}` = source.`{column}`" for column in key_columns)
    changed_predicate = " OR ".join(
        f"NOT (target.`{column}` <=> source.`{column}`)"
        for column in expected_columns
        if column not in key_columns
    )
    (
        target.alias("target")
        .merge(frame.alias("source"), predicate)
        .whenMatchedUpdateAll(condition=changed_predicate)
        .whenNotMatchedInsertAll()
        .execute()
    )
