"""Deterministic local Spark/Delta construction of ERCOT hourly Gold V1."""

from __future__ import annotations

from functools import reduce
from pathlib import Path
from typing import Any

from texas_energy.data.gold import (
    CANONICAL_DEMAND_UNIT,
    CANONICAL_WEATHER_CITIES,
    CANONICAL_WEATHER_UNITS,
    GOLD_CONTRACT_VERSION,
    HISTORICAL_OBSERVED_WEATHER,
    GoldFeatureConfig,
)
from texas_energy.data.silver import EIA_DATASET
from texas_energy.spark.silver import (
    SparkDependencyError,
    _delta_columns,
    _is_delta_table,
    _optional_delta_table,
    _spark_runtime,
    _sql_merge,
    delta_location,
)


class GoldValidationError(ValueError):
    """Silver input cannot safely produce a deterministic Gold feature table."""


class GoldSchemaMigrationRequired(RuntimeError):
    """An existing Gold Delta output cannot safely accept this contract version."""


_DEMAND_REQUIRED_COLUMNS = frozenset(
    {
        "source",
        "dataset",
        "period_utc",
        "respondent",
        "demand_type",
        "demand_value",
        "demand_unit",
        "source_request_fingerprint",
        "source_raw_payload_sha256",
        "sanitized_payload_sha256",
        "ingested_at_utc",
        "bronze_uri",
        "quality_status",
        "quality_flags",
    }
)
_WEATHER_REQUIRED_COLUMNS = frozenset(
    {
        "source",
        "dataset",
        "city",
        "observed_at_utc",
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "temperature_unit",
        "relative_humidity_unit",
        "wind_speed_unit",
        "requested_latitude",
        "requested_longitude",
        "provider_latitude",
        "provider_longitude",
        "provider_coordinate_distance_km",
        "source_request_fingerprint",
        "source_raw_payload_sha256",
        "sanitized_payload_sha256",
        "ingested_at_utc",
        "bronze_uri",
        "quality_status",
        "quality_flags",
    }
)


def run_local_gold_job(
    *,
    silver_input: Path,
    gold_output: Path,
    config: GoldFeatureConfig = GoldFeatureConfig(),
) -> int:
    """Run the reviewed local Spark/Delta implementation unchanged."""

    return run_gold_job(silver_input=silver_input, gold_output=gold_output, config=config)


def run_gold_job(
    *,
    silver_input: Path | str,
    gold_output: Path | str,
    config: GoldFeatureConfig = GoldFeatureConfig(),
    spark: Any | None = None,
    delta_table: Any | None = None,
) -> int:
    """Read accepted Silver Delta and deterministically write Gold V1 Delta.

    ``gold_output`` is a distinct Delta table path. This local MVP deliberately
    never writes to either Silver input table. The normalized weather-lineage
    table is written separately at ``<gold_output>-weather-lineage``; Delta
    cannot atomically commit both tables. If a first write fails after the
    feature table commits but before lineage commits, do not repair that
    feature-only directory in place: build a new candidate path. If both
    tables already exist, rerunning the same input is idempotent and repairs a
    later failed lineage merge by re-merging both source snapshots.
    """

    # Gold is a derived, versioned feature contract: it retains Silver lineage
    # while enforcing point-in-time-safe demand and weather feature semantics.

    if not _is_uri(silver_input) and not Path(silver_input).is_dir():
        raise GoldValidationError("Silver input directory does not exist.")
    owns_spark = spark is None
    if spark is None:
        spark, delta_table, _ = _spark_runtime()
    elif delta_table is None:
        delta_table = _optional_delta_table()
    try:
        demand = _read_delta(spark, _child_location(silver_input, "demand"), "demand")
        weather = _read_delta(spark, _child_location(silver_input, "weather_historical"), "historical weather")
        gold = build_gold_features(
            spark=spark,
            demand=demand,
            historical_weather=weather,
            config=config,
        )
        weather_lineage = build_gold_weather_lineage(
            spark=spark,
            features=gold,
            historical_weather=weather,
        )
        row_count = gold.count()  # Complete all source validation before any target-table write.
        _require_compatible_gold_output(spark, delta_table, gold_output)
        _merge_gold_table(spark, delta_table, gold, gold_output)
        lineage_output = _weather_lineage_output(gold_output)
        _merge_gold_weather_lineage(spark, delta_table, weather_lineage, lineage_output)
        _validate_persisted_gold_lineage(spark, gold_output, lineage_output)
        return row_count
    finally:
        if owns_spark:
            spark.stop()


def run_core_demand_gold_job(
    *,
    silver_input: Path | str,
    gold_output: Path | str,
    observed_weather_lineage_output: Path | str | None = None,
    config: GoldFeatureConfig = GoldFeatureConfig(),
    spark: Any | None = None,
    delta_table: Any | None = None,
) -> int:
    """Write the demand-only Gold product without requiring observed weather.

    Historical observed weather remains a separate validated enrichment product.
    This function is deliberately not a serving-weather substitute: prospective
    weather-aware models continue to require their issued-weather contract.
    """

    if not _is_uri(silver_input) and not Path(silver_input).is_dir():
        raise GoldValidationError("Silver input directory does not exist.")
    owns_spark = spark is None
    if spark is None:
        spark, delta_table, _ = _spark_runtime()
    elif delta_table is None:
        delta_table = _optional_delta_table()
    try:
        demand = _read_delta(spark, _child_location(silver_input, "demand"), "demand")
        core = build_core_demand_gold_features(spark=spark, demand=demand, config=config)
        row_count = core.count()
        if observed_weather_lineage_output is not None:
            _require_compatible_core_gold_output(
                spark,
                delta_table,
                gold_output,
                observed_weather_lineage_output,
            )
        # This is the causal core-demand contract, not the weather-enriched
        # ``run_gold_job`` pair.  Do not reject a valid demand-only table just
        # because a separate observed-weather product is incomplete.
        _merge_gold_table(spark, delta_table, core, gold_output)
        if observed_weather_lineage_output is not None:
            historical_weather = _read_delta(
                spark,
                _child_location(silver_input, "weather_historical"),
                "historical weather",
            )
            lineage = build_observed_weather_lineage(
                spark=spark,
                historical_weather=historical_weather,
            )
            _merge_observed_weather_lineage(
                spark,
                delta_table,
                lineage,
                observed_weather_lineage_output,
            )
        return row_count
    finally:
        if owns_spark:
            spark.stop()


def build_gold_features(*, spark: Any, demand: Any, historical_weather: Any, config: GoldFeatureConfig) -> Any:
    """Validate Silver V2 and return one causal Gold feature row per demand hour."""

    from pyspark.sql import Window, functions as F

    _validate_demand(spark, demand)
    _validate_historical_weather(spark, historical_weather)

    base = demand.select(
        F.col("period_utc"),
        F.col("source").alias("demand_source"),
        F.col("dataset").alias("demand_dataset"),
        F.col("respondent"),
        F.col("demand_type"),
        F.col("demand_value").alias("observed_demand_value_mwh"),
        F.col("demand_unit"),
        # The divisor is deliberately literal and explicit: EIA has validated
        # each source record as exactly one hourly MWh interval.
        (F.col("demand_value") / F.lit(1)).cast("decimal(38,12)").alias("observed_average_demand_mw"),
        F.col("source_request_fingerprint").alias("demand_source_request_fingerprint"),
        F.col("source_raw_payload_sha256").alias("demand_source_raw_payload_sha256"),
        F.col("sanitized_payload_sha256").alias("demand_sanitized_payload_sha256"),
        F.col("ingested_at_utc").alias("demand_ingested_at_utc"),
        F.col("bronze_uri").alias("demand_bronze_uri"),
    )
    joined = base
    for city in CANONICAL_WEATHER_CITIES:
        city_weather = historical_weather.filter(F.col("city") == city).select(
            F.col("observed_at_utc").alias("period_utc"),
            F.col("temperature_2m").alias(f"{city}_observed_temperature_2m_c"),
            F.col("relative_humidity_2m").alias(f"{city}_observed_relative_humidity_pct"),
            F.col("wind_speed_10m").alias(f"{city}_observed_wind_speed_10m_kmh"),
        )
        joined = joined.join(city_weather, on="period_utc", how="left")

    weather_columns = [
        f"{city}_observed_{feature}"
        for city in CANONICAL_WEATHER_CITIES
        for feature in ("temperature_2m_c", "relative_humidity_pct", "wind_speed_10m_kmh")
    ]
    _require_no_rows(
        joined.filter(reduce(lambda left, right: left | right, (F.col(column).isNull() for column in weather_columns))),
        "Historical weather is missing at least one configured city-hour for a demand timestamp.",
    )

    features = (
        joined.withColumn("gold_contract_version", F.lit(GOLD_CONTRACT_VERSION))
        .withColumn("weather_availability", F.lit(HISTORICAL_OBSERVED_WEATHER))
        .withColumn("utc_hour", F.hour("period_utc"))
        # Spark dayofweek is explicit here: Sunday=1 through Saturday=7.
        .withColumn("utc_day_of_week_sun1", F.dayofweek("period_utc"))
        .withColumn("utc_month", F.month("period_utc"))
        .withColumn("is_weekend_utc", F.dayofweek("period_utc").isin(1, 7))
    )
    history = Window.orderBy("period_utc")
    # Missing source hours are preserved.  A segment starts after every UTC
    # discontinuity so no lag, trailing window, or later model context can
    # treat observations across a gap as adjacent elapsed hours.
    segmented = (
        features.withColumn("_previous_period_utc", F.lag("period_utc").over(history))
        .withColumn(
            "_segment_start",
            F.when(
                F.col("_previous_period_utc").isNull()
                | (F.unix_timestamp("period_utc") - F.unix_timestamp("_previous_period_utc") != 3600),
                F.lit(1),
            ).otherwise(F.lit(0)),
        )
        .withColumn(
            "_segment_id",
            F.sum("_segment_start").over(history.rowsBetween(Window.unboundedPreceding, Window.currentRow)),
        )
    )
    segment_history = Window.partitionBy("_segment_id").orderBy("period_utc")
    features = segmented
    for hours in config.lag_hours:
        features = features.withColumn(
            f"demand_lag_{hours}h_mwh",
            F.lag("observed_demand_value_mwh", hours).over(segment_history),
        )
    for hours in config.trailing_window_hours:
        trailing = segment_history.rowsBetween(-hours, -1)
        trailing_observation_count = F.count("observed_demand_value_mwh").over(trailing)
        features = features.withColumn(
            f"demand_trailing_mean_{hours}h_mwh",
            # A partial window is warm-up data, not a fully populated
            # trailing feature. The preceding continuity validation makes a
            # count of exactly ``hours`` mean exactly that many elapsed UTC
            # hours, with no hidden gap crossed by the rolling feature.
            F.when(
                trailing_observation_count == hours,
                F.avg("observed_demand_value_mwh").over(trailing),
            ).cast("decimal(38,12)"),
        )
    return features.drop("_previous_period_utc", "_segment_start", "_segment_id").orderBy("period_utc")


def build_core_demand_gold_features(*, spark: Any, demand: Any, config: GoldFeatureConfig) -> Any:
    """Build the causal core-demand Gold product with no observed-weather join.

    This is the product used to establish historical demand continuity and
    demand-derived features.  It intentionally has no observed-weather columns
    or weather lineage, so missing actual weather is reported by the separate
    enrichment product instead of blocking unrelated demand processing.
    """

    from pyspark.sql import Window, functions as F

    _validate_demand(spark, demand)
    features = (
        demand.select(
            F.col("period_utc"),
            F.col("source").alias("demand_source"),
            F.col("dataset").alias("demand_dataset"),
            F.col("respondent"),
            F.col("demand_type"),
            F.col("demand_value").alias("observed_demand_value_mwh"),
            F.col("demand_unit"),
            (F.col("demand_value") / F.lit(1)).cast("decimal(38,12)").alias("observed_average_demand_mw"),
            F.col("source_request_fingerprint").alias("demand_source_request_fingerprint"),
            F.col("source_raw_payload_sha256").alias("demand_source_raw_payload_sha256"),
            F.col("sanitized_payload_sha256").alias("demand_sanitized_payload_sha256"),
            F.col("ingested_at_utc").alias("demand_ingested_at_utc"),
            F.col("bronze_uri").alias("demand_bronze_uri"),
        )
        .withColumn("gold_contract_version", F.lit(GOLD_CONTRACT_VERSION))
        .withColumn("weather_availability", F.lit("not_required_for_core_demand"))
        .withColumn("utc_hour", F.hour("period_utc"))
        .withColumn("utc_day_of_week_sun1", F.dayofweek("period_utc"))
        .withColumn("utc_month", F.month("period_utc"))
        .withColumn("is_weekend_utc", F.dayofweek("period_utc").isin(1, 7))
    )
    history = Window.orderBy("period_utc")
    segmented = (
        features.withColumn("_previous_period_utc", F.lag("period_utc").over(history))
        .withColumn(
            "_segment_start",
            F.when(
                F.col("_previous_period_utc").isNull()
                | (F.unix_timestamp("period_utc") - F.unix_timestamp("_previous_period_utc") != 3600),
                F.lit(1),
            ).otherwise(F.lit(0)),
        )
        .withColumn(
            "_segment_id",
            F.sum("_segment_start").over(history.rowsBetween(Window.unboundedPreceding, Window.currentRow)),
        )
    )
    segment_history = Window.partitionBy("_segment_id").orderBy("period_utc")
    for hours in config.lag_hours:
        segmented = segmented.withColumn(
            f"demand_lag_{hours}h_mwh",
            F.lag("observed_demand_value_mwh", hours).over(segment_history),
        )
    for hours in config.trailing_window_hours:
        trailing = segment_history.rowsBetween(-hours, -1)
        segmented = segmented.withColumn(
            f"demand_trailing_mean_{hours}h_mwh",
            F.when(
                F.count("observed_demand_value_mwh").over(trailing) == hours,
                F.avg("observed_demand_value_mwh").over(trailing),
            ).cast("decimal(38,12)"),
        )
    return segmented.drop("_previous_period_utc", "_segment_start", "_segment_id").orderBy("period_utc")


def build_observed_weather_lineage(*, spark: Any, historical_weather: Any) -> Any:
    """Preserve genuine observed-weather coverage without inventing city-hours.

    Core demand features remain available when actual weather is incomplete.
    This separate Delta product records only validated source observations; it
    is evidence of partial coverage, never a substitute for issued forecast
    weather in the prospective model contract.
    """

    from pyspark.sql import functions as F

    _validate_historical_weather(spark, historical_weather)
    lineage = historical_weather.select(
        F.col("observed_at_utc").alias("period_utc"),
        "city",
        "source",
        "dataset",
        "temperature_2m",
        "relative_humidity_2m",
        "wind_speed_10m",
        "temperature_unit",
        "relative_humidity_unit",
        "wind_speed_unit",
        "source_request_fingerprint",
        "source_raw_payload_sha256",
        "sanitized_payload_sha256",
        "ingested_at_utc",
        "bronze_uri",
        "quality_status",
        "quality_flags",
    )
    _require_no_rows(
        lineage.groupBy("period_utc", "city").count().filter("count != 1"),
        "Observed-weather lineage must contain at most one validated record per city-hour.",
    )
    return lineage.orderBy("period_utc", "city")


def build_gold_weather_lineage(*, spark: Any, features: Any, historical_weather: Any) -> Any:
    """Return one verifiable weather-source reference per Gold hour and city.

    Weather provenance remains normalized so the model-facing feature table
    stays one row per ERCOT hour. ``weather_lineage_reference`` is a
    deterministic hash of the source business key and source provenance, not a
    replacement for the individual provenance columns.
    """

    from pyspark.sql import functions as F

    _validate_historical_weather(spark, historical_weather)
    lineage = historical_weather.select(
        F.col("observed_at_utc").alias("period_utc"),
        F.col("city"),
        F.col("source").alias("weather_source"),
        F.col("dataset").alias("weather_dataset"),
        F.col("source_request_fingerprint").alias("weather_source_request_fingerprint"),
        F.col("source_raw_payload_sha256").alias("weather_source_raw_payload_sha256"),
        F.col("sanitized_payload_sha256").alias("weather_sanitized_payload_sha256"),
        F.col("ingested_at_utc").alias("weather_ingested_at_utc"),
        F.col("bronze_uri").alias("weather_bronze_uri"),
    )
    # Weather remains available for a missing-demand hour, but Gold has no
    # feature row there. Keep lineage exactly aligned to feature timestamps;
    # this neither drops source weather from Silver nor fabricates demand.
    lineage = lineage.join(features.select("period_utc").distinct(), on="period_utc", how="left_semi")
    lineage = lineage.withColumn(
        "weather_lineage_reference",
        F.sha2(
            F.to_json(
                F.struct(
                    F.col("period_utc"),
                    F.col("city"),
                    F.col("weather_source"),
                    F.col("weather_dataset"),
                    F.col("weather_source_request_fingerprint"),
                    F.col("weather_source_raw_payload_sha256"),
                    F.col("weather_sanitized_payload_sha256"),
                    F.col("weather_bronze_uri"),
                )
            ),
            256,
        ),
    )
    _validate_candidate_weather_lineage(features, lineage)
    return lineage.orderBy("period_utc", "city")


def _read_delta(spark: Any, table_path: Path | str, label: str) -> Any:
    if not _is_uri(table_path) and not Path(table_path).is_dir():
        raise GoldValidationError(f"Silver {label} Delta table does not exist.")
    return spark.read.format("delta").load(delta_location(table_path))


def _validate_demand(spark: Any, demand: Any) -> None:
    from pyspark.sql import functions as F

    _require_columns(demand, _DEMAND_REQUIRED_COLUMNS, "Silver demand")
    _require_no_rows(
        demand.filter(
            F.col("period_utc").isNull()
            | F.col("respondent").isNull()
            | F.col("demand_type").isNull()
            | F.col("demand_value").isNull()
            | F.col("demand_unit").isNull()
            | F.col("quality_status").isNull()
        ),
        "Silver demand contains null required values.",
    )
    _require_no_rows(
        demand.filter(
            (F.col("source") != "eia")
            | (F.col("dataset") != EIA_DATASET)
            | (F.col("respondent") != "ERCO")
            | (F.col("demand_type") != "D")
            | (F.col("demand_unit") != CANONICAL_DEMAND_UNIT)
            | (F.col("demand_value") < 0)
            | (F.col("quality_status") != "valid")
            | (F.size("quality_flags") != 0)
        ),
        "Silver demand violates the validated EIA ERCO/D/megawatthours contract.",
    )
    _require_no_rows(
        demand.groupBy("source", "dataset", "period_utc").count().filter(F.col("count") > 1),
        "Silver demand has duplicate business keys.",
    )
    _require_no_rows(
        demand.filter(F.date_trunc("hour", F.col("period_utc")) != F.col("period_utc")),
        "Silver demand timestamps must align exactly to UTC hours.",
    )
    # Gaps are a source-quality fact, not a reason to fabricate or discard
    # valid history. ``build_gold_features`` creates segment-local temporal
    # windows so lag/rolling features cannot cross one.


def _validate_historical_weather(spark: Any, weather: Any) -> None:
    from pyspark.sql import functions as F

    _require_columns(weather, _WEATHER_REQUIRED_COLUMNS, "Silver historical weather")
    _require_no_rows(
        weather.filter(
            F.col("city").isNull()
            | F.col("observed_at_utc").isNull()
            | F.col("temperature_2m").isNull()
            | F.col("relative_humidity_2m").isNull()
            | F.col("wind_speed_10m").isNull()
            | F.col("requested_latitude").isNull()
            | F.col("requested_longitude").isNull()
            | F.col("provider_latitude").isNull()
            | F.col("provider_longitude").isNull()
            | F.col("provider_coordinate_distance_km").isNull()
            | F.col("source_request_fingerprint").isNull()
            | F.col("source_raw_payload_sha256").isNull()
            | F.col("sanitized_payload_sha256").isNull()
            | F.col("ingested_at_utc").isNull()
            | F.col("bronze_uri").isNull()
            | F.col("quality_status").isNull()
        ),
        "Silver historical weather contains null required values.",
    )
    _require_no_rows(
        weather.filter(
            (F.col("source") != "open_meteo")
            | (F.col("dataset") != "historical-weather")
            | (~F.col("city").isin(*CANONICAL_WEATHER_CITIES))
            | (F.col("temperature_unit") != CANONICAL_WEATHER_UNITS["temperature_2m"])
            | (F.col("relative_humidity_unit") != CANONICAL_WEATHER_UNITS["relative_humidity_2m"])
            | (F.col("wind_speed_unit") != CANONICAL_WEATHER_UNITS["wind_speed_10m"])
            | (F.col("requested_latitude") < -90)
            | (F.col("requested_latitude") > 90)
            | (F.col("provider_latitude") < -90)
            | (F.col("provider_latitude") > 90)
            | (F.col("requested_longitude") < -180)
            | (F.col("requested_longitude") > 180)
            | (F.col("provider_longitude") < -180)
            | (F.col("provider_longitude") > 180)
            | (F.col("provider_coordinate_distance_km") < 0)
            # Silver validates source/model-specific grid bounds. Gold only
            # receives validated rows, but retains the ERA5 upper invariant.
            | (F.col("provider_coordinate_distance_km") > 20)
            | (F.col("relative_humidity_2m") < 0)
            | (F.col("relative_humidity_2m") > 100)
            | (F.col("wind_speed_10m") < 0)
            | (F.col("quality_status") != "valid")
            | (F.size("quality_flags") != 0)
            | F.isnan("temperature_2m")
            | F.isnan("relative_humidity_2m")
            | F.isnan("wind_speed_10m"),
        ),
        "Silver historical weather violates the validated Gold weather contract.",
    )
    _require_no_rows(
        weather.filter(F.date_trunc("hour", F.col("observed_at_utc")) != F.col("observed_at_utc")),
        "Silver historical weather timestamps must align exactly to UTC hours.",
    )
    _require_no_rows(
        weather.groupBy("city", "dataset", "observed_at_utc").count().filter(F.col("count") > 1),
        "Silver historical weather has duplicate city-hour business keys.",
    )
    actual_cities = {row.city for row in weather.select("city").distinct().collect()}
    if actual_cities != set(CANONICAL_WEATHER_CITIES):
        raise GoldValidationError("Silver historical weather must contain exactly the five configured Texas cities.")


def _require_columns(frame: Any, required: frozenset[str], label: str) -> None:
    missing = required.difference(frame.columns)
    if missing:
        raise GoldValidationError(f"{label} is missing required columns: {', '.join(sorted(missing))}.")


def _require_no_rows(frame: Any, message: str) -> None:
    if frame.limit(1).count():
        raise GoldValidationError(message)


def _validate_candidate_weather_lineage(features: Any, lineage: Any) -> None:
    from pyspark.sql import functions as F

    _require_no_rows(
        lineage.groupBy("period_utc", "city").count().filter(F.col("count") > 1),
        "Gold weather lineage has duplicate period/city keys.",
    )
    expected_periods = features.select("period_utc").distinct()
    _require_no_rows(
        expected_periods.join(lineage.select("period_utc").distinct(), on="period_utc", how="left_anti"),
        "Gold weather lineage is missing a feature-table timestamp.",
    )
    _require_no_rows(
        lineage.select("period_utc").distinct().join(expected_periods, on="period_utc", how="left_anti"),
        "Gold weather lineage contains a timestamp absent from the feature table.",
    )
    _require_no_rows(
        lineage.join(expected_periods, on="period_utc", how="left_semi")
        .groupBy("period_utc")
        .agg(F.count("*").alias("city_count"), F.countDistinct("city").alias("distinct_city_count"))
        .filter((F.col("city_count") != len(CANONICAL_WEATHER_CITIES)) | (F.col("distinct_city_count") != len(CANONICAL_WEATHER_CITIES))),
        "Every Gold feature timestamp must have exactly five weather-lineage records.",
    )


def _merge_gold_table(spark: Any, delta_table: Any, features: Any, gold_output: Path | str) -> None:
    """Idempotently merge Gold rows after source validation has completed."""

    _ensure_local_parent(gold_output)
    table_uri = delta_location(gold_output)
    if not _is_delta_table(spark, delta_table, table_uri):
        features.write.format("delta").mode("errorifexists").save(table_uri)
        return
    expected_columns = tuple(features.columns)
    if _delta_columns(spark, delta_table, table_uri) != expected_columns:
        raise GoldSchemaMigrationRequired(
            "Existing Gold Delta schema does not match Gold V1. Build to a new output directory, validate it, then perform an approved cutover."
        )
    if delta_table is None:
        _sql_merge(
            spark,
            features,
            table_path=table_uri,
            key_columns=("period_utc",),
            expected_columns=expected_columns,
        )
        return
    target = delta_table.forPath(spark, table_uri)
    predicate = "target.`period_utc` = source.`period_utc`"
    changed = " OR ".join(
        f"NOT (target.`{column}` <=> source.`{column}`)"
        for column in expected_columns
        if column != "period_utc"
    )
    (
        target.alias("target")
        .merge(features.alias("source"), predicate)
        .whenMatchedUpdateAll(condition=changed)
        .whenNotMatchedInsertAll()
        .execute()
    )


def _merge_gold_weather_lineage(spark: Any, delta_table: Any, lineage: Any, lineage_output: Path | str) -> None:
    """Idempotently merge normalized weather provenance by timestamp and city."""

    _ensure_local_parent(lineage_output)
    table_uri = delta_location(lineage_output)
    if not _is_delta_table(spark, delta_table, table_uri):
        lineage.write.format("delta").mode("errorifexists").save(table_uri)
        return
    expected_columns = tuple(lineage.columns)
    if _delta_columns(spark, delta_table, table_uri) != expected_columns:
        raise GoldSchemaMigrationRequired(
            "Existing Gold weather-lineage schema does not match this reviewed contract. Build to a new output directory."
        )
    if delta_table is None:
        _sql_merge(
            spark,
            lineage,
            table_path=table_uri,
            key_columns=("period_utc", "city"),
            expected_columns=expected_columns,
        )
        return
    target = delta_table.forPath(spark, table_uri)
    changed = " OR ".join(
        f"NOT (target.`{column}` <=> source.`{column}`)"
        for column in expected_columns
        if column not in {"period_utc", "city"}
    )
    (
        target.alias("target")
        .merge(lineage.alias("source"), "target.`period_utc` = source.`period_utc` AND target.`city` = source.`city`")
        .whenMatchedUpdateAll(condition=changed)
        .whenNotMatchedInsertAll()
        .execute()
    )


def _merge_observed_weather_lineage(spark: Any, delta_table: Any, lineage: Any, lineage_output: Path | str) -> None:
    """Merge the independent observed-weather coverage product by city-hour."""

    _ensure_local_parent(lineage_output)
    table_uri = delta_location(lineage_output)
    if not _is_delta_table(spark, delta_table, table_uri):
        lineage.write.format("delta").mode("errorifexists").save(table_uri)
        return
    expected_columns = tuple(lineage.columns)
    if _delta_columns(spark, delta_table, table_uri) != expected_columns:
        raise GoldSchemaMigrationRequired(
            "Existing observed-weather lineage schema does not match the reviewed core-demand contract. Build to a new output directory."
        )
    if delta_table is None:
        _sql_merge(
            spark,
            lineage,
            table_path=table_uri,
            key_columns=("period_utc", "city"),
            expected_columns=expected_columns,
        )
        return
    target = delta_table.forPath(spark, table_uri)
    predicate = "target.`period_utc` = source.`period_utc` AND target.`city` = source.`city`"
    changed = " OR ".join(
        f"NOT (target.`{column}` <=> source.`{column}`)"
        for column in expected_columns
        if column not in {"period_utc", "city"}
    )
    (
        target.alias("target")
        .merge(lineage.alias("source"), predicate)
        .whenMatchedUpdateAll(condition=changed)
        .whenNotMatchedInsertAll()
        .execute()
    )
def _require_compatible_gold_output(spark: Any, delta_table: Any, gold_output: Path | str) -> None:
    """Prevent an in-place migration of a pre-lineage Gold V1 directory."""

    feature_exists = _is_delta_table(spark, delta_table, delta_location(gold_output))
    lineage_exists = _is_delta_table(spark, delta_table, delta_location(_weather_lineage_output(gold_output)))
    if feature_exists != lineage_exists:
        raise GoldSchemaMigrationRequired(
            "Gold output has only one of the required feature and weather-lineage Delta tables. "
            "Do not modify it in place; build and validate a new candidate directory instead."
        )


def _require_compatible_core_gold_output(
    spark: Any,
    delta_table: Any,
    gold_output: Path | str,
    observed_weather_lineage_output: Path | str,
) -> None:
    """Reject a partially published v2 core-demand release before any write."""

    features_exist = _is_delta_table(spark, delta_table, delta_location(gold_output))
    lineage_exists = _is_delta_table(
        spark,
        delta_table,
        delta_location(observed_weather_lineage_output),
    )
    if features_exist != lineage_exists:
        raise GoldSchemaMigrationRequired(
            "Core-demand Gold v2 has only one required Delta product. Do not repair it in place; build and validate a new versioned release."
        )


def _validate_persisted_gold_lineage(spark: Any, gold_output: Path | str, lineage_output: Path | str) -> None:
    """Verify post-write feature/lineage cardinality without claiming atomicity."""

    features = _read_delta(spark, gold_output, "Gold features")
    lineage = _read_delta(spark, lineage_output, "Gold weather lineage")
    _validate_candidate_weather_lineage(features, lineage)


def _weather_lineage_output(gold_output: Path | str) -> Path | str:
    """Keep the lineage Delta table outside the feature Delta table directory.

    Delta treats child paths below a table root as partition fragments, so a
    sibling table is required for an independently versioned normalized table.
    """

    if _is_uri(gold_output):
        value = str(gold_output).rstrip("/")
        parent, _, name = value.rpartition("/")
        return f"{parent}/{name}-weather-lineage"
    output = Path(gold_output)
    return output.with_name(f"{output.name}-weather-lineage")


def _is_uri(value: Path | str) -> bool:
    return "://" in str(value)


def _child_location(parent: Path | str, child: str) -> Path | str:
    value = str(parent)
    return f"{value.rstrip('/')}/{child}" if _is_uri(value) else Path(value) / child


def _ensure_local_parent(location: Path | str) -> None:
    if not _is_uri(location):
        Path(location).parent.mkdir(parents=True, exist_ok=True)
