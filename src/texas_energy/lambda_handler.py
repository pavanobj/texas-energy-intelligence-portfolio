"""Thin, validated AWS Lambda adapter for Texas Energy ingestion and scheduling.

The handler accepts only bounded source-ingestion operations plus one narrow
EventBridge scheduling operation.  The latter derives a deterministic daily
Step Functions input; it never runs Glue or SageMaker itself.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
import json
import os
from typing import Literal

from .ingestion.bronze import BronzeS3Writer
from .ingestion.credentials import DEFAULT_AWS_REGION, get_eia_api_key_from_ssm
from .ingestion.eia import EiaDemandClient, EiaDemandQuery
from .ingestion.errors import IngestionError, SourceDataUnavailableError
from .ingestion.http import RetryingJsonClient, RetryPolicy, UrllibTransport
from .ingestion.open_meteo import WeatherClient
from .ingestion.open_meteo import TexasCity
from .ingestion.orchestration import BronzeIngestionResult, IngestionOrchestrator


Operation = Literal[
    "eia_ercot_demand",
    "dallas_historical_weather",
    "dallas_forecast_weather",
    "texas_historical_weather",
    "texas_forecast_weather",
    "texas_issued_ifs_weather",
    "start_production_pipeline",
]

EIA_OPERATION: Operation = "eia_ercot_demand"
HISTORICAL_WEATHER_OPERATION: Operation = "dallas_historical_weather"
FORECAST_WEATHER_OPERATION: Operation = "dallas_forecast_weather"
TEXAS_HISTORICAL_WEATHER_OPERATION: Operation = "texas_historical_weather"
TEXAS_FORECAST_WEATHER_OPERATION: Operation = "texas_forecast_weather"
TEXAS_ISSUED_IFS_WEATHER_OPERATION: Operation = "texas_issued_ifs_weather"
START_PRODUCTION_PIPELINE_OPERATION: Operation = "start_production_pipeline"

MAX_EIA_WINDOW = timedelta(days=7)
MAX_EIA_PAGE_SIZE = 500
DEFAULT_EIA_PAGE_SIZE = 168
MAX_HISTORICAL_WINDOW = timedelta(days=31)
DEFAULT_FORECAST_HOURS = 24
MAX_FORECAST_HOURS = 48
MAX_HTTP_RESPONSE_BYTES = 4 * 1024 * 1024


class LambdaEventValidationError(IngestionError):
    """The Lambda invocation did not meet the intentionally narrow input contract."""


class LambdaConfigurationError(IngestionError):
    """A required non-secret Lambda configuration value is unavailable."""


@dataclass(frozen=True)
class Invocation:
    """One validated operation with provider-specific, bounded parameters."""

    operation: Operation
    eia_query: EiaDemandQuery | None = None
    historical_start_date: date | None = None
    historical_end_date: date | None = None
    forecast_hours: int | None = None
    model_run_utc: datetime | None = None
    scheduled_at_utc: datetime | None = None
    allow_empty: bool = False


@dataclass(frozen=True)
class LambdaDependencies:
    """Injectable runtime edges; tests never create AWS clients or HTTP sockets."""

    s3_client: object
    http_client: RetryingJsonClient
    eia_api_key_provider: Callable[[], str]
    bronze_bucket_name: str
    weather_retrieved_at: Callable[[], datetime]
    state_machine_client: object | None = None
    production_state_machine_arn: str | None = None


def handler(
    event: object,
    context: object | None,
    *,
    dependencies: LambdaDependencies | None = None,
) -> dict[str, object]:
    """Validate an explicit ingestion event and return a safe persistence summary.

    Lambda invokes this function with two positional arguments.  The keyword-only
    ``dependencies`` seam exists solely for local tests and is never populated by
    the Lambda runtime.
    """

    del context
    invocation = parse_invocation(event)
    runtime = dependencies or production_dependencies()
    if invocation.operation == START_PRODUCTION_PIPELINE_OPERATION:
        return _start_production_pipeline(invocation, runtime)

    # Production flow writes immutable canonical Bronze envelopes; downstream Glue
    # receives their S3 references rather than raw provider payloads in workflow state.
    orchestrator = build_orchestrator(invocation, runtime)

    if invocation.operation == EIA_OPERATION:
        assert invocation.eia_query is not None
        results = orchestrator.ingest_eia_ercot_demand(
            invocation.eia_query,
            allow_empty=invocation.allow_empty,
        )
    elif invocation.operation in {HISTORICAL_WEATHER_OPERATION, TEXAS_HISTORICAL_WEATHER_OPERATION}:
        assert invocation.historical_start_date is not None
        assert invocation.historical_end_date is not None
        is_production_five_city = invocation.operation == TEXAS_HISTORICAL_WEATHER_OPERATION
        cities = tuple(TexasCity) if is_production_five_city else (TexasCity.DALLAS,)
        try:
            if is_production_five_city:
                results = orchestrator.ingest_open_meteo_historical_cities(
                    cities=cities,
                    start_date=invocation.historical_start_date,
                    end_date=invocation.historical_end_date,
                    require_complete_hours=True,
                )
            else:
                results = (
                    orchestrator.ingest_open_meteo_historical(
                        city=TexasCity.DALLAS,
                        start_date=invocation.historical_start_date,
                        end_date=invocation.historical_end_date,
                    ),
                )
        except SourceDataUnavailableError as error:
            if not is_production_five_city:
                raise
            return _historical_weather_unavailable(invocation, error)
    elif invocation.operation in {FORECAST_WEATHER_OPERATION, TEXAS_FORECAST_WEATHER_OPERATION}:
        assert invocation.forecast_hours is not None
        cities = (TexasCity.DALLAS,) if invocation.operation == FORECAST_WEATHER_OPERATION else tuple(TexasCity)
        results = tuple(
            orchestrator.ingest_open_meteo_forecast(city=city, forecast_hours=invocation.forecast_hours)
            for city in cities
        )
    else:
        assert invocation.forecast_hours is not None
        assert invocation.model_run_utc is not None
        results = tuple(
            orchestrator.ingest_open_meteo_issued_ifs(
                city=city,
                model_run_utc=invocation.model_run_utc,
                forecast_hours=invocation.forecast_hours,
            )
            for city in TexasCity
        )

    summaries = [_result_summary(result) for result in results]
    return {
        "operation": invocation.operation,
        "status": "completed",
        "empty_response_accepted": invocation.allow_empty and not results,
        "results": summaries,
        "manifest": {
            "schema_version": "bronze-ingestion-manifest-v1",
            "code_revision": os.environ.get("DEPLOYMENT_REVISION", "unverified"),
            "bronze_objects": summaries,
        },
    }


def parse_invocation(event: object) -> Invocation:
    """Parse only supported operations and bounded source windows before I/O."""

    if not isinstance(event, Mapping):
        raise LambdaEventValidationError("Lambda event must be a JSON object.")
    operation = event.get("operation")
    if operation == EIA_OPERATION:
        _only_allowed_fields(event, {"operation", "start", "end", "page_size", "allow_empty"})
        start = _parse_utc_hour(event.get("start"), "start")
        end = _parse_utc_hour(event.get("end"), "end")
        page_size = _bounded_int(
            event.get("page_size", DEFAULT_EIA_PAGE_SIZE),
            "page_size",
            minimum=1,
            maximum=MAX_EIA_PAGE_SIZE,
        )
        allow_empty = _boolean(event.get("allow_empty", False), "allow_empty")
        try:
            query = EiaDemandQuery(start=start, end=end, page_size=page_size)
        except ValueError as error:
            raise LambdaEventValidationError("EIA date window or page size is invalid.") from None
        if query.end - query.start > MAX_EIA_WINDOW:
            raise LambdaEventValidationError("EIA date window exceeds the seven-day Lambda limit.")
        return Invocation(operation=EIA_OPERATION, eia_query=query, allow_empty=allow_empty)

    if operation in {HISTORICAL_WEATHER_OPERATION, TEXAS_HISTORICAL_WEATHER_OPERATION}:
        _only_allowed_fields(event, {"operation", "start_date", "end_date"})
        start_date = _parse_date(event.get("start_date"), "start_date")
        end_date = _parse_date(event.get("end_date"), "end_date")
        if end_date < start_date or end_date - start_date > MAX_HISTORICAL_WINDOW:
            raise LambdaEventValidationError("Historical weather date window is invalid or exceeds 31 days.")
        return Invocation(
            operation=operation,
            historical_start_date=start_date,
            historical_end_date=end_date,
        )

    if operation in {FORECAST_WEATHER_OPERATION, TEXAS_FORECAST_WEATHER_OPERATION}:
        _only_allowed_fields(event, {"operation", "forecast_hours"})
        forecast_hours = _bounded_int(
            event.get("forecast_hours", DEFAULT_FORECAST_HOURS),
            "forecast_hours",
            minimum=1,
            maximum=MAX_FORECAST_HOURS,
        )
        return Invocation(operation=operation, forecast_hours=forecast_hours)

    if operation == TEXAS_ISSUED_IFS_WEATHER_OPERATION:
        _only_allowed_fields(event, {"operation", "model_run_utc", "forecast_hours"})
        model_run_utc = _parse_utc_hour(event.get("model_run_utc"), "model_run_utc")
        if model_run_utc.hour != 0:
            raise LambdaEventValidationError("model_run_utc must be an exact 00:00 UTC ECMWF IFS run.")
        forecast_hours = _bounded_int(
            event.get("forecast_hours", MAX_FORECAST_HOURS),
            "forecast_hours",
            minimum=24,
            maximum=MAX_FORECAST_HOURS,
        )
        return Invocation(
            operation=operation,
            forecast_hours=forecast_hours,
            model_run_utc=model_run_utc,
        )

    if operation == START_PRODUCTION_PIPELINE_OPERATION:
        _only_allowed_fields(event, {"operation", "scheduled_at_utc"})
        scheduled_at = _parse_utc_hour(event.get("scheduled_at_utc"), "scheduled_at_utc")
        if scheduled_at.hour != 15:
            raise LambdaEventValidationError("scheduled_at_utc must be the 15:00 UTC daily production schedule.")
        return Invocation(operation=START_PRODUCTION_PIPELINE_OPERATION, scheduled_at_utc=scheduled_at)

    raise LambdaEventValidationError("Lambda event operation is unsupported.")


def production_dependencies() -> LambdaDependencies:
    """Create production SDK and HTTP boundaries using Lambda's default credentials."""

    bucket_name = os.environ.get("BRONZE_BUCKET_NAME", "").strip()
    if not bucket_name:
        raise LambdaConfigurationError("BRONZE_BUCKET_NAME must be configured.")
    try:
        import boto3
        from botocore.config import Config
    except ImportError:
        raise LambdaConfigurationError("boto3 is required by the Lambda runtime.") from None

    client_config = Config(
        retries={"total_max_attempts": 2, "mode": "standard"},
        connect_timeout=5,
        read_timeout=10,
    )
    return LambdaDependencies(
        s3_client=boto3.client("s3", region_name=DEFAULT_AWS_REGION, config=client_config),
        http_client=RetryingJsonClient(
            UrllibTransport(max_response_bytes=MAX_HTTP_RESPONSE_BYTES),
            retry_policy=RetryPolicy(max_attempts=3, max_retry_after_seconds=20),
        ),
        eia_api_key_provider=lambda: get_eia_api_key_from_ssm(region_name=DEFAULT_AWS_REGION),
        bronze_bucket_name=bucket_name,
        weather_retrieved_at=lambda: datetime.now(timezone.utc),
        state_machine_client=boto3.client("stepfunctions", region_name=DEFAULT_AWS_REGION, config=client_config),
        production_state_machine_arn=os.environ.get("PRODUCTION_PIPELINE_STATE_MACHINE_ARN", "").strip() or None,
    )


def _start_production_pipeline(invocation: Invocation, dependencies: LambdaDependencies) -> dict[str, object]:
    """Start exactly one daily normal execution from the EventBridge schedule.

    The execution name is the immutable issue identity.  EventBridge retries can
    therefore observe an already-started execution rather than creating duplicate
    Glue or SageMaker work for the same forecast issue.
    """

    assert invocation.scheduled_at_utc is not None
    if dependencies.state_machine_client is None or not dependencies.production_state_machine_arn:
        raise LambdaConfigurationError("Production scheduling requires a state-machine client and ARN.")

    issue = invocation.scheduled_at_utc.astimezone(timezone.utc)
    issue_id = issue.strftime("%Y%m%dT%H%M%SZ")
    context_end = issue - timedelta(hours=24)
    # Open-Meteo's ERA5 archive can publish completed observations after the
    # daily forecast workflow fires. Source checks on 2026-10-29 proved that
    # Oct 23 was complete for all five cities while Oct 24 still had no values.
    # Keep one additional full calendar day of margin so the strict atomic
    # five-city capture never discards an otherwise valid 14-day batch because
    # its newest archive day is still pending.
    observed_weather_end = issue.date() - timedelta(days=6)
    execution_input = {
        "forecast_issue_id": issue_id,
        "forecast_origin_utc": _format_utc(issue),
        "weather_model_run_utc": _format_utc(issue.replace(hour=0)),
        # EIA publication is delayed.  Re-ingest the bounded seven-day source
        # window ending at the causal 24-hour context cutoff; Glue then proves
        # the required 2,048-hour Gold continuity before model work starts.
        "demand_start_utc": _format_utc(context_end - timedelta(days=7)),
        "demand_end_utc": _format_utc(context_end),
        # Each normal run refreshes the same rolling evidence horizon. Recent
        # target hours remain pending until a later run can obtain a complete,
        # genuine archive response for every city.
        "weather_start_date": (observed_weather_end - timedelta(days=13)).isoformat(),
        "weather_end_date": observed_weather_end.isoformat(),
    }
    execution_name = f"scheduled-{issue_id.lower()}"
    try:
        response = dependencies.state_machine_client.start_execution(  # type: ignore[union-attr]
            stateMachineArn=dependencies.production_state_machine_arn,
            name=execution_name,
            input=json.dumps(execution_input, separators=(",", ":")),
        )
    except Exception as error:
        response_data = getattr(error, "response", {})
        error_code = response_data.get("Error", {}).get("Code") if isinstance(response_data, Mapping) else None
        if error_code == "ExecutionAlreadyExists":
            return {
                "operation": START_PRODUCTION_PIPELINE_OPERATION,
                "status": "already_started",
                "forecast_issue_id": issue_id,
                "execution_name": execution_name,
            }
        raise
    return {
        "operation": START_PRODUCTION_PIPELINE_OPERATION,
        "status": "started",
        "forecast_issue_id": issue_id,
        "execution_name": execution_name,
        "execution_arn": response["executionArn"],
    }


def build_orchestrator(invocation: Invocation, dependencies: LambdaDependencies) -> IngestionOrchestrator:
    """Compose provider clients lazily so weather operations never read the EIA key."""

    if invocation.operation == EIA_OPERATION:
        eia_api_key = dependencies.eia_api_key_provider()
    else:
        eia_api_key = "unused-for-weather-operations"
    return IngestionOrchestrator(
        eia_client=EiaDemandClient(dependencies.http_client, api_key=eia_api_key),
        weather_client=WeatherClient(
            dependencies.http_client,
            retrieved_at=dependencies.weather_retrieved_at,
        ),
        bronze_writer=BronzeS3Writer(dependencies.s3_client, bucket_name=dependencies.bronze_bucket_name),
    )


def _only_allowed_fields(event: Mapping[object, object], allowed: set[str]) -> None:
    if any(not isinstance(key, str) or key not in allowed for key in event):
        raise LambdaEventValidationError("Lambda event contains unsupported fields.")


def _parse_utc_hour(value: object, field_name: str) -> datetime:
    if not isinstance(value, str):
        raise LambdaEventValidationError(f"{field_name} must be an ISO-8601 UTC timestamp.")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        raise LambdaEventValidationError(f"{field_name} must be an ISO-8601 UTC timestamp.") from None
    if parsed.tzinfo is None:
        raise LambdaEventValidationError(f"{field_name} must be timezone-aware and in UTC.")
    normalized = parsed.astimezone(timezone.utc)
    if normalized.minute or normalized.second or normalized.microsecond:
        raise LambdaEventValidationError(f"{field_name} must align to a UTC hour.")
    return normalized


def _parse_date(value: object, field_name: str) -> date:
    if not isinstance(value, str):
        raise LambdaEventValidationError(f"{field_name} must use YYYY-MM-DD.")
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise LambdaEventValidationError(f"{field_name} must use YYYY-MM-DD.") from None


def _bounded_int(value: object, field_name: str, *, minimum: int, maximum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
        raise LambdaEventValidationError(f"{field_name} must be an integer within the supported range.")
    return value


def _boolean(value: object, field_name: str) -> bool:
    if not isinstance(value, bool):
        raise LambdaEventValidationError(f"{field_name} must be a boolean.")
    return value


def _result_summary(result: BronzeIngestionResult) -> dict[str, object]:
    return {
        "provider": result.provider,
        "dataset": result.dataset,
        "object_key": result.object_key,
        "record_count": result.record_count,
        "source_time_range_utc": {
            "start": _format_utc(result.source_time_range.start),
            "end": _format_utc(result.source_time_range.end),
        },
        "already_exists": result.already_exists,
        "page_offset": result.page_offset,
    }


def _historical_weather_unavailable(invocation: Invocation, error: SourceDataUnavailableError) -> dict[str, object]:
    """Return an honest optional-enrichment outcome without invalid Bronze writes."""

    assert invocation.historical_start_date is not None
    assert invocation.historical_end_date is not None
    return {
        "operation": TEXAS_HISTORICAL_WEATHER_OPERATION,
        "status": "source_data_unavailable",
        "results": [],
        "availability": {
            "provider": "open_meteo",
            "dataset": "historical-weather",
            "requested_start_date": invocation.historical_start_date.isoformat(),
            "requested_end_date": invocation.historical_end_date.isoformat(),
            "reason": str(error),
        },
        "manifest": {
            "schema_version": "bronze-ingestion-manifest-v1",
            "code_revision": os.environ.get("DEPLOYMENT_REVISION", "unverified"),
            "bronze_objects": [],
        },
    }


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
