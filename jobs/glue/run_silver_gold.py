"""Run the reviewed Bronze -> Silver -> Gold contract in a Glue-compatible Spark runtime.

This entrypoint deliberately receives all locations as parameters.  It has no
AWS SDK calls, credentials, or Windows-path assumptions; Glue resolves S3
access through the job role and local Docker runs can bind-mount fixture paths.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from texas_energy.data.gold import GoldFeatureConfig
from texas_energy.spark.gold import _weather_lineage_output, run_gold_job
from texas_energy.spark.silver import delta_location, run_silver_job


# These are injected by the managed Glue 5 launcher, rather than supplied by
# callers of this application.  Keep this list explicit: argparse must still
# reject misspelled transformation options and arbitrary unknown arguments.
_GLUE_PLATFORM_VALUE_ARGUMENTS = (
    "--JOB_ID",
    "--JOB_RUN_ID",
    "--glue-di-packages-correlation-ids",
    "--internal-lib-urls",
    "--tenant-internal",
)


def _hours(value: str) -> tuple[int, ...]:
    try:
        values = tuple(int(item) for item in value.split(",") if item)
    except ValueError as error:
        raise argparse.ArgumentTypeError("hour values must be comma-separated integers") from error
    if not values or any(item <= 0 for item in values) or len(set(values)) != len(values):
        raise argparse.ArgumentTypeError("hour values must be positive and unique")
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bronze-input", required=True, help="Local path or S3 URI containing Bronze envelope JSON files.")
    parser.add_argument("--silver-output", required=True, help="Local path or S3 URI for Silver Delta tables.")
    parser.add_argument("--gold-output", required=True, help="Local path or S3 URI for the Gold Delta feature table.")
    # AWS Glue injects this platform argument for Spark jobs. It is metadata
    # rather than a transformation input, so retaining it avoids a parser
    # mismatch without coupling this reusable runner to GlueContext.
    parser.add_argument("--JOB_NAME", required=False, help=argparse.SUPPRESS)
    for argument in _GLUE_PLATFORM_VALUE_ARGUMENTS:
        parser.add_argument(argument, required=False, help=argparse.SUPPRESS)
    parser.add_argument("--artifact-package-sha256", required=False, help=argparse.SUPPRESS)
    parser.add_argument("--artifact-script-sha256", required=False, help=argparse.SUPPRESS)
    parser.add_argument("--lag-hours", type=_hours, default=(1, 24, 168))
    parser.add_argument("--trailing-window-hours", type=_hours, default=(24, 168))
    parser.add_argument(
        "--shuffle-partitions",
        type=int,
        default=4,
        help="Spark SQL shuffle partitions; tune for the Glue worker count and input size.",
    )
    return parser


def _runtime_versions(spark: object) -> dict[str, str]:
    java = spark.sparkContext._jvm.java.lang.System.getProperty("java.version")
    try:
        import delta

        delta_version = str(getattr(delta, "__version__", "unknown"))
    except ImportError:
        # Glue 5 supplies the Delta JVM libraries, not the local delta-spark
        # Python module. The documented Glue 5 Delta version is reported by
        # the execution manifest rather than pretending a Python package exists.
        delta_version = "Glue JVM Delta (no delta-spark Python module)"
    return {
        "python": sys.version.split()[0],
        "spark": spark.version,
        "delta_python": delta_version,
        "java": str(java),
    }


def _validate_delta_configuration(spark: object) -> None:
    extensions = spark.conf.get("spark.sql.extensions", "")
    catalog = spark.conf.get("spark.sql.catalog.spark_catalog", "")
    if "io.delta.sql.DeltaSparkSessionExtension" not in extensions or catalog != "org.apache.spark.sql.delta.catalog.DeltaCatalog":
        raise RuntimeError(
            "Delta is not configured. Submit with the Glue Delta job configuration "
            "(--datalake-formats delta and the documented Delta Spark conf)."
        )


def main() -> int:
    args = _parser().parse_args()
    if args.shuffle_partitions <= 0:
        raise ValueError("--shuffle-partitions must be positive")
    from pyspark.sql import SparkSession, functions as F

    spark = SparkSession.builder.getOrCreate()
    try:
        # This job consumes canonical Bronze envelopes and writes only derived
        # Silver/Gold Delta tables, preserving the artifact hashes in its run evidence.
        spark.conf.set("spark.sql.session.timeZone", "UTC")
        spark.conf.set("spark.sql.shuffle.partitions", str(args.shuffle_partitions))
        _validate_delta_configuration(spark)
        # Glue 5 exposes Delta through Spark/JVM libraries, but not the
        # optional local ``delta-spark`` Python wrapper.  Passing ``None``
        # selects the SQL MERGE adapter while retaining the same Delta tables.
        delta_table = None
        silver_counts = run_silver_job(
            bronze_input=args.bronze_input,
            silver_output=args.silver_output,
            spark=spark,
            delta_table=delta_table,
        )
        gold_count = run_gold_job(
            silver_input=args.silver_output,
            gold_output=args.gold_output,
            config=GoldFeatureConfig(lag_hours=args.lag_hours, trailing_window_hours=args.trailing_window_hours),
            spark=spark,
            delta_table=delta_table,
        )
        demand = spark.read.format("delta").load(delta_location(_child(args.silver_output, "demand")))
        gold = spark.read.format("delta").load(delta_location(args.gold_output))
        lineage = spark.read.format("delta").load(delta_location(_weather_lineage_output(args.gold_output)))
        if demand.filter(F.col("demand_unit") != "megawatthours").limit(1).count():
            raise RuntimeError("Silver demand unit contract was not preserved.")
        if demand.select("period_utc").distinct().count() != demand.count():
            raise RuntimeError("Silver demand has duplicate hourly keys.")
        if demand.filter(
            F.col("period_utc").isNull()
            | F.col("demand_value").isNull()
            | F.col("demand_unit").isNull()
        ).limit(1).count():
            raise RuntimeError("Silver demand has a null required contract field.")
        demand_periods = [row.period_utc for row in demand.select("period_utc").orderBy("period_utc").collect()]
        if any((right - left).total_seconds() != 3600 for left, right in zip(demand_periods, demand_periods[1:])):
            raise RuntimeError("Silver demand timestamps are not consecutive UTC hours.")
        if gold.select("period_utc").distinct().count() != gold.count():
            raise RuntimeError("Gold has duplicate hourly keys.")
        if gold.filter(
            F.col("period_utc").isNull() | F.col("observed_average_demand_mw").isNull()
        ).limit(1).count():
            raise RuntimeError("Gold has a null required contract field.")
        if lineage.groupBy("period_utc").count().filter(F.col("count") != 5).limit(1).count():
            raise RuntimeError("Gold weather lineage does not contain five records per Gold hour.")
        print(
            json.dumps(
                {
                    "artifact": {
                        "package_sha256": args.artifact_package_sha256,
                        "script_sha256": args.artifact_script_sha256,
                    },
                    "runtime": _runtime_versions(spark),
                    "silver_rows": silver_counts,
                    "gold_rows": gold_count,
                    "readback": {
                        "silver_demand": demand.count(),
                        "gold_features": gold.count(),
                        "gold_weather_lineage": lineage.count(),
                    },
                    "schemas": {
                        "silver_demand": demand.schema.simpleString(),
                        "gold": gold.schema.simpleString(),
                        "gold_weather_lineage": lineage.schema.simpleString(),
                    },
                },
                sort_keys=True,
            )
        )
        return 0
    finally:
        spark.stop()


def _child(parent: str, child: str) -> str | Path:
    return f"{parent.rstrip('/')}/{child}" if "://" in parent else Path(parent) / child


def _run_glue_entrypoint() -> None:
    """Run successfully without raising SystemExit, which Glue records as a failure."""
    status = main()
    if status not in (None, 0):
        raise RuntimeError(f"Glue entrypoint returned nonzero status: {status}")


if __name__ == "__main__":
    _run_glue_entrypoint()
