"""Glue 5 production entrypoint for canonical Bronze -> Silver -> Gold Delta."""

from __future__ import annotations

import argparse
import json

from texas_energy.spark.dashboard_evaluation import publish_dashboard_evaluation
from texas_energy.spark.production import run_production_silver_gold_job


# Glue 5 adds these launcher-owned arguments after Terraform's application
# arguments.  Accept only this observed, documented platform set: unknown
# application flags must still fail closed instead of being silently ignored.
_GLUE_PLATFORM_VALUE_ARGUMENTS = (
    "--JOB_ID",
    "--JOB_RUN_ID",
    "--glue-di-packages-correlation-ids",
    "--internal-lib-urls",
    "--tenant-internal",
)


def _hours(value: str) -> tuple[int, ...]:
    values = tuple(int(item) for item in value.split(",") if item)
    if not values or any(item <= 0 for item in values) or len(values) != len(set(values)):
        raise argparse.ArgumentTypeError("hour values must be positive and unique")
    return values


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bronze-input", required=True)
    parser.add_argument("--silver-output", required=True)
    parser.add_argument("--gold-output", required=True)
    parser.add_argument("--gold-weather-lineage-output", required=False)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--lag-hours", type=_hours, default=(1, 24, 168))
    parser.add_argument("--trailing-window-hours", type=_hours, default=(24, 168))
    parser.add_argument("--shuffle-partitions", type=int, default=64)
    parser.add_argument("--forecast-input-output", required=False)
    parser.add_argument("--forecast-origin-utc", required=False)
    parser.add_argument("--weather-model-run-utc", required=False)
    parser.add_argument("--forecast-issue-id", required=False)
    parser.add_argument("--dashboard-evaluation-only", choices=("true", "false"), default="false")
    parser.add_argument("--dashboard-output", required=False)
    parser.add_argument("--forecast-output", required=False)
    parser.add_argument(
        "--forecast-generation-type",
        choices=("aws_managed_prospective_input", "aws_managed_historical_replay_input"),
        default="aws_managed_prospective_input",
    )
    parser.add_argument("--JOB_NAME", required=False, help=argparse.SUPPRESS)
    for argument in _GLUE_PLATFORM_VALUE_ARGUMENTS:
        parser.add_argument(argument, required=False, help=argparse.SUPPRESS)
    return parser


def main() -> int:
    args = _parser().parse_args()
    if args.shuffle_partitions <= 0:
        raise ValueError("--shuffle-partitions must be positive")
    from pyspark.sql import SparkSession

    spark = SparkSession.builder.getOrCreate()
    try:
        spark.conf.set("spark.sql.session.timeZone", "UTC")
        spark.conf.set("spark.sql.shuffle.partitions", str(args.shuffle_partitions))
        if args.dashboard_evaluation_only == "true":
            required = (args.dashboard_output, args.forecast_output, args.forecast_input_output, args.gold_weather_lineage_output)
            if not all(required):
                raise ValueError("dashboard evaluation requires dashboard, forecast, input, and observed-weather output locations")
            counts = publish_dashboard_evaluation(
                spark=spark,
                forecast_output=args.forecast_output,
                input_root=args.forecast_input_output,
                gold_output=args.gold_output,
                weather_gold=args.gold_weather_lineage_output,
                dashboard_output=args.dashboard_output,
            )
            print(json.dumps({"run_id": args.run_id, "dashboard_evaluation": counts}, sort_keys=True))
            return 0
        counts = run_production_silver_gold_job(
            bronze_input=args.bronze_input,
            silver_output=args.silver_output,
            gold_output=args.gold_output,
            observed_weather_lineage_output=args.gold_weather_lineage_output,
            spark=spark,
            lag_hours=args.lag_hours,
            trailing_window_hours=args.trailing_window_hours,
            forecast_input_output=args.forecast_input_output,
            forecast_origin_utc=args.forecast_origin_utc,
            weather_model_run_utc=args.weather_model_run_utc,
            forecast_issue_id=args.forecast_issue_id,
            forecast_generation_type=args.forecast_generation_type,
        )
        print(json.dumps({"run_id": args.run_id, "counts": counts}, sort_keys=True))
        return 0
    finally:
        spark.stop()


if __name__ == "__main__":
    status = main()
    if status:
        raise RuntimeError(f"Glue production entrypoint returned {status}")
