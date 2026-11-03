"""Mock-only tests for the Lambda adapter around the existing ingestion pipeline."""

from __future__ import annotations

from collections import deque
from collections.abc import Callable
from datetime import datetime, timezone
import json
import traceback

import pytest

from texas_energy.lambda_handler import LambdaDependencies, LambdaEventValidationError, handler
from texas_energy.ingestion.errors import BronzePersistenceError, EmptySourceResponseError
from texas_energy.ingestion.http import HttpRequest, HttpResponse, RetryingJsonClient


class ScriptedTransport:
    """In-memory transport used to keep Lambda adapter tests offline."""

    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = deque(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("Unexpected HTTP request.")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload: object) -> HttpResponse:
    return HttpResponse(
        status_code=200,
        headers={"content-type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )


class FakeS3Client:
    def __init__(self, outcome: Exception | None = None) -> None:
        self.outcome = outcome
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.outcome is not None:
            raise self.outcome
        return {"ETag": '"test"'}


class FakeStateMachineClient:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def start_execution(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        return {"executionArn": "arn:example:states:us-east-1:<AWS_ACCOUNT_ID>:execution:pipeline:scheduled-test"}


def _dependencies(
    responses: list[HttpResponse | Exception],
    *,
    s3_outcome: Exception | None = None,
    key_provider: Callable[[], str] | None = None,
) -> tuple[LambdaDependencies, FakeS3Client, ScriptedTransport]:
    transport = ScriptedTransport(responses)
    s3 = FakeS3Client(s3_outcome)
    return (
        LambdaDependencies(
            s3_client=s3,
            http_client=RetryingJsonClient(transport),
            eia_api_key_provider=key_provider or (lambda: "not-a-real-eia-key"),
            bronze_bucket_name="texas-energy-bronze-example",
            weather_retrieved_at=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        s3,
        transport,
    )


def _eia_response() -> HttpResponse:
    return json_response(
        {
            "response": {
                "total": "1",
                "data": [
                    {
                        "period": "2024-01-01T00",
                        "respondent": "ERCO",
                        "type": "D",
                        "value": "45000",
                        "value-units": "megawatthours",
                    }
                ],
            }
        }
    )


def _weather_response() -> HttpResponse:
    return json_response(
        {
            "utc_offset_seconds": 0,
            "hourly_units": {
                "temperature_2m": "°C",
                "relative_humidity_2m": "%",
                "wind_speed_10m": "km/h",
            },
            "hourly": {
                "time": [1_704_067_200, 1_704_070_800],
                "temperature_2m": [10.0, 11.0],
                "relative_humidity_2m": [50, 51],
                "wind_speed_10m": [12.0, 13.0],
            },
        }
    )


def _unavailable_weather_response() -> HttpResponse:
    payload = json.loads(_weather_response().body)
    for field in ("temperature_2m", "relative_humidity_2m", "wind_speed_10m"):
        payload["hourly"][field] = [None, None]
    return json_response(payload)


def _issued_ifs_weather_response() -> HttpResponse:
    hours = list(range(24))
    payload = {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "apparent_temperature": "°C",
            "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": [1_704_067_200 + hour * 3600 for hour in hours],
            "temperature_2m": [10.0 + hour for hour in hours],
            "relative_humidity_2m": [50 + hour for hour in hours],
            "apparent_temperature": [9.0 + hour for hour in hours],
            "wind_speed_10m": [12.0 + hour for hour in hours],
        },
    }
    return json_response(payload)


def test_eia_handler_persists_a_validated_capture_without_exposing_the_api_key() -> None:
    credentials_requested = 0

    def key_provider() -> str:
        nonlocal credentials_requested
        credentials_requested += 1
        return "mock-eia-key"

    dependencies, s3, transport = _dependencies([_eia_response()], key_provider=key_provider)

    response = handler(
        {
            "operation": "eia_ercot_demand",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T00:00:00Z",
            "page_size": 1,
        },
        None,
        dependencies=dependencies,
    )

    assert credentials_requested == 1
    assert len(transport.requests) == 1
    assert len(s3.calls) == 1
    assert response["operation"] == "eia_ercot_demand"
    assert response["results"][0]["record_count"] == 1
    assert "mock-eia-key" not in json.dumps(response)
    assert "mock-eia-key" not in s3.calls[0]["Body"].decode("utf-8")


def test_weather_operations_never_request_the_eia_credential() -> None:
    def key_provider() -> str:
        pytest.fail("weather operations must not read the EIA SSM credential")

    historical_dependencies, historical_s3, historical_transport = _dependencies(
        [_weather_response()],
        key_provider=key_provider,
    )
    historical = handler(
        {
            "operation": "dallas_historical_weather",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        },
        None,
        dependencies=historical_dependencies,
    )
    forecast_dependencies, forecast_s3, forecast_transport = _dependencies(
        [_weather_response()],
        key_provider=key_provider,
    )
    forecast = handler(
        {"operation": "dallas_forecast_weather", "forecast_hours": 2},
        None,
        dependencies=forecast_dependencies,
    )

    assert historical["results"][0]["dataset"] == "historical-weather"
    assert forecast["results"][0]["dataset"] == "forecast"
    assert len(historical_transport.requests) == len(forecast_transport.requests) == 1
    assert len(historical_s3.calls) == len(forecast_s3.calls) == 1


def test_issued_ifs_operation_persists_five_run_identified_city_captures() -> None:
    dependencies, s3, transport = _dependencies([_issued_ifs_weather_response()] * 5)

    result = handler(
        {
            "operation": "texas_issued_ifs_weather",
            "model_run_utc": "2024-01-01T00:00:00Z",
            "forecast_hours": 24,
        },
        None,
        dependencies=dependencies,
    )

    assert result["operation"] == "texas_issued_ifs_weather"
    assert len(result["results"]) == len(s3.calls) == len(transport.requests) == 5
    assert all(request.parameters["models"] == "ecmwf_ifs" for request in transport.requests)
    assert all(summary["dataset"] == "issued-forecast-weather" for summary in result["results"])


def test_five_city_historical_weather_reports_unavailable_source_without_bronze_writes() -> None:
    dependencies, s3, transport = _dependencies([_unavailable_weather_response()] * 5)

    result = handler(
        {
            "operation": "texas_historical_weather",
            "start_date": "2024-01-01",
            "end_date": "2024-01-01",
        },
        None,
        dependencies=dependencies,
    )

    assert result["status"] == "source_data_unavailable"
    assert result["results"] == []
    assert result["availability"]["provider"] == "open_meteo"
    assert s3.calls == []
    assert len(transport.requests) == 5


def test_daily_schedule_starts_one_causal_normal_pipeline_execution() -> None:
    dependencies, s3, transport = _dependencies([])
    state_machine = FakeStateMachineClient()
    dependencies = LambdaDependencies(
        s3_client=dependencies.s3_client,
        http_client=dependencies.http_client,
        eia_api_key_provider=dependencies.eia_api_key_provider,
        bronze_bucket_name=dependencies.bronze_bucket_name,
        weather_retrieved_at=dependencies.weather_retrieved_at,
        state_machine_client=state_machine,
        production_state_machine_arn="arn:example:states:us-east-1:<AWS_ACCOUNT_ID>:stateMachine:texas-energy-production-data-pipeline",
    )

    result = handler(
        {"operation": "start_production_pipeline", "scheduled_at_utc": "2026-10-29T15:00:00Z"},
        None,
        dependencies=dependencies,
    )

    assert result["status"] == "started"
    assert result["forecast_issue_id"] == "20261029T150000Z"
    assert s3.calls == [] and transport.requests == []
    assert len(state_machine.calls) == 1
    call = state_machine.calls[0]
    assert call["name"] == "scheduled-20261029t150000z"
    payload = json.loads(str(call["input"]))
    assert payload == {
        "forecast_issue_id": "20261029T150000Z",
        "forecast_origin_utc": "2026-10-29T15:00:00Z",
        "weather_model_run_utc": "2026-10-29T00:00:00Z",
        "demand_start_utc": "2026-10-21T15:00:00Z",
        "demand_end_utc": "2026-10-28T15:00:00Z",
        "weather_start_date": "2026-10-10",
        "weather_end_date": "2026-10-23",
    }


def test_daily_schedule_keeps_the_newest_unavailable_era5_day_pending() -> None:
    """A strict 14-day five-city batch must end before the verified archive lag."""

    dependencies, _, _ = _dependencies([])
    state_machine = FakeStateMachineClient()
    dependencies = LambdaDependencies(
        s3_client=dependencies.s3_client,
        http_client=dependencies.http_client,
        eia_api_key_provider=dependencies.eia_api_key_provider,
        bronze_bucket_name=dependencies.bronze_bucket_name,
        weather_retrieved_at=dependencies.weather_retrieved_at,
        state_machine_client=state_machine,
        production_state_machine_arn="arn:example:states:us-east-1:<AWS_ACCOUNT_ID>:stateMachine:texas-energy-production-data-pipeline",
    )

    handler(
        {"operation": "start_production_pipeline", "scheduled_at_utc": "2026-10-29T15:00:00Z"},
        None,
        dependencies=dependencies,
    )

    payload = json.loads(str(state_machine.calls[0]["input"]))
    assert payload["weather_start_date"] == "2026-10-10"
    assert payload["weather_end_date"] == "2026-10-23"
    assert payload["weather_end_date"] != "2026-10-24"


@pytest.mark.parametrize(
    "event",
    [
        {},
        {"operation": "unknown"},
        {"operation": "eia_ercot_demand", "start": "2024-01-01T00:30:00Z", "end": "2024-01-01T01:00:00Z"},
        {"operation": "eia_ercot_demand", "start": "2024-01-01T00:00:00Z", "end": "2024-01-09T00:00:00Z"},
        {"operation": "eia_ercot_demand", "start": "2024-01-01T00:00:00Z", "end": "2024-01-01T00:00:00Z", "page_size": 501},
        {"operation": "dallas_historical_weather", "start_date": "2024-01-01", "end_date": "2024-02-03"},
        {"operation": "dallas_forecast_weather", "forecast_hours": 49},
        {"operation": "texas_issued_ifs_weather", "model_run_utc": "2024-01-01T01:00:00Z"},
        {"operation": "dallas_forecast_weather", "unexpected": "value"},
    ],
)
def test_invalid_events_fail_before_aws_or_provider_calls(event: object) -> None:
    dependencies, s3, transport = _dependencies([])

    with pytest.raises(LambdaEventValidationError):
        handler(event, None, dependencies=dependencies)

    assert s3.calls == []
    assert transport.requests == []


def test_empty_eia_is_a_safe_failure_without_a_bronze_write() -> None:
    dependencies, s3, _ = _dependencies([json_response({"response": {"total": "0", "data": []}})])

    with pytest.raises(EmptySourceResponseError):
        handler(
            {
                "operation": "eia_ercot_demand",
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-01T00:00:00Z",
            },
            None,
            dependencies=dependencies,
        )

    assert s3.calls == []


def test_allowed_empty_eia_is_explicit_and_never_writes() -> None:
    dependencies, s3, _ = _dependencies([json_response({"response": {"total": "0", "data": []}})])

    response = handler(
        {
            "operation": "eia_ercot_demand",
            "start": "2024-01-01T00:00:00Z",
            "end": "2024-01-01T00:00:00Z",
            "allow_empty": True,
        },
        None,
        dependencies=dependencies,
    )

    assert response["empty_response_accepted"] is True
    assert response["results"] == []
    assert s3.calls == []


def test_bronze_failures_are_safe_in_lambda_tracebacks() -> None:
    secret = "test-secret-that-must-not-be-disclosed"
    dependencies, _, _ = _dependencies([_eia_response()], s3_outcome=RuntimeError(secret))

    with pytest.raises(BronzePersistenceError) as raised:
        handler(
            {
                "operation": "eia_ercot_demand",
                "start": "2024-01-01T00:00:00Z",
                "end": "2024-01-01T00:00:00Z",
            },
            None,
            dependencies=dependencies,
        )

    assert secret not in str(raised.value)
    assert secret not in "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )
