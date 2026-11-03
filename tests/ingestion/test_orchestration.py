"""Mocked end-to-end tests for local ingestion orchestration."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json

import pytest

from texas_energy.ingestion.bronze import BronzeS3Writer
from texas_energy.ingestion.eia import EiaDemandClient, EiaDemandQuery
from texas_energy.ingestion.errors import BronzePersistenceError, ContractError, EmptySourceResponseError
from texas_energy.ingestion.http import RetryingJsonClient
from texas_energy.ingestion.open_meteo import TexasCity, WeatherClient
from texas_energy.ingestion.orchestration import IngestionOrchestrator

from .conftest import ScriptedTransport, json_response


class FakeS3Client:
    def __init__(self, outcomes: list[Exception | None] | None = None) -> None:
        self._outcomes = list(outcomes or [])
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if self._outcomes:
            outcome = self._outcomes.pop(0)
            if outcome is not None:
                raise outcome
        return {"ETag": '"mock"'}


class FakeS3ClientError(Exception):
    def __init__(self, *, code: str, status_code: int) -> None:
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }
        super().__init__("mock S3 failure")


class RecoverableFakeS3Client:
    """Model immutable S3 storage with one planned write failure."""

    def __init__(self, *, fail_on_put_number: int) -> None:
        self._fail_on_put_number = fail_on_put_number
        self.calls: list[dict[str, object]] = []
        self.objects: dict[str, bytes] = {}

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if len(self.calls) == self._fail_on_put_number:
            raise RuntimeError("planned transient S3 failure")
        key = kwargs["Key"]
        body = kwargs["Body"]
        assert isinstance(key, str)
        assert isinstance(body, bytes)
        if key in self.objects:
            raise FakeS3ClientError(code="PreconditionFailed", status_code=412)
        self.objects[key] = body
        return {"ETag": '"mock"'}


def _eia_record(hour: int, *, unit: str = "megawatthours") -> dict[str, object]:
    return {
        "period": f"2024-01-01T{hour:02d}",
        "respondent": "ERCO",
        "respondent-name": "Electric Reliability Council of Texas, Inc.",
        "type": "D",
        "type-name": "Demand",
        "value": "45000",
        "value-units": unit,
    }


def _weather_payload() -> dict[str, object]:
    return {
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


def _issued_ifs_payload() -> dict[str, object]:
    payload = _weather_payload()
    payload["hourly_units"]["apparent_temperature"] = "°C"  # type: ignore[index]
    payload["hourly"]["apparent_temperature"] = [9.0, 10.0]  # type: ignore[index]
    return payload


def _orchestrator(
    *,
    eia_responses: list[object],
    weather_responses: list[object],
    s3_outcomes: list[Exception | None] | None = None,
) -> tuple[IngestionOrchestrator, FakeS3Client, ScriptedTransport, ScriptedTransport]:
    eia_transport = ScriptedTransport(eia_responses)  # type: ignore[arg-type]
    weather_transport = ScriptedTransport(weather_responses)  # type: ignore[arg-type]
    eia_client = EiaDemandClient(
        RetryingJsonClient(eia_transport),
        api_key="mock-api-key",
        retrieved_at=lambda: datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
    )
    weather_client = WeatherClient(
        RetryingJsonClient(weather_transport),
        retrieved_at=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )
    s3_client = FakeS3Client(s3_outcomes)
    return (
        IngestionOrchestrator(
            eia_client=eia_client,
            weather_client=weather_client,
            bronze_writer=BronzeS3Writer(s3_client, bucket_name="texas-energy-bronze-example"),
        ),
        s3_client,
        eia_transport,
        weather_transport,
    )


def _query(*, page_size: int = 2) -> EiaDemandQuery:
    return EiaDemandQuery(
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        page_size=page_size,
    )


def _eia_orchestrator_with_s3(
    responses: list[object],
    s3_client: object,
) -> tuple[IngestionOrchestrator, ScriptedTransport]:
    transport = ScriptedTransport(responses)  # type: ignore[arg-type]
    return (
        IngestionOrchestrator(
            eia_client=EiaDemandClient(
                RetryingJsonClient(transport),
                api_key="mock-api-key",
                retrieved_at=lambda: datetime(2024, 1, 1, 3, tzinfo=timezone.utc),
            ),
            weather_client=WeatherClient(RetryingJsonClient(ScriptedTransport([]))),
            bronze_writer=BronzeS3Writer(s3_client, bucket_name="texas-energy-bronze-example"),  # type: ignore[arg-type]
        ),
        transport,
    )


def test_ingests_one_validated_eia_page_to_one_bronze_object() -> None:
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[json_response({"response": {"total": "1", "data": [_eia_record(0)]}})],
        weather_responses=[],
    )

    results = orchestrator.ingest_eia_ercot_demand(_query(page_size=1))

    assert len(results) == 1
    assert results[0].provider == "eia"
    assert results[0].dataset == "electricity/rto/region-data"
    assert results[0].record_count == 1
    assert results[0].page_offset == 0
    assert results[0].source_time_range.start == datetime(2024, 1, 1, tzinfo=timezone.utc)
    envelope = json.loads(s3.calls[0]["Body"])
    assert envelope["units"] == {"value": "megawatthours"}
    assert envelope["source_payload"]["response"]["data"][0]["period"] == "2024-01-01T00"


def test_ingests_each_non_empty_eia_page_as_its_own_bronze_object() -> None:
    orchestrator, s3, eia_transport, _ = _orchestrator(
        eia_responses=[
            json_response({"response": {"total": "3", "data": [_eia_record(0), _eia_record(1)]}}),
            json_response({"response": {"total": "3", "data": [_eia_record(2)]}}),
        ],
        weather_responses=[],
    )

    results = orchestrator.ingest_eia_ercot_demand(_query())

    assert [result.page_offset for result in results] == [0, 2]
    assert [result.record_count for result in results] == [2, 1]
    assert results[0].object_key != results[1].object_key
    assert [request.parameters["offset"] for request in eia_transport.requests] == ["0", "2"]
    first_envelope = json.loads(s3.calls[0]["Body"])
    second_envelope = json.loads(s3.calls[1]["Body"])
    assert first_envelope["capture"]["request_parameters"]["offset"] == "0"
    assert second_envelope["capture"]["request_parameters"]["offset"] == "2"
    assert first_envelope["source_time_range_utc"]["end"] == "2024-01-01T01:00:00Z"
    assert second_envelope["source_time_range_utc"]["start"] == "2024-01-01T02:00:00Z"


def test_ingests_dallas_historical_weather_using_validated_timestamps_and_units() -> None:
    orchestrator, s3, _, weather_transport = _orchestrator(
        eia_responses=[],
        weather_responses=[json_response(_weather_payload())],
    )

    result = orchestrator.ingest_open_meteo_dallas_historical(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
    )

    assert result.provider == "open_meteo"
    assert result.dataset == "historical-weather"
    assert result.record_count == 2
    assert weather_transport.requests[0].parameters["latitude"] == str(TexasCity.DALLAS.latitude)
    envelope = json.loads(s3.calls[0]["Body"])
    assert envelope["units"] == _weather_payload()["hourly_units"]
    assert envelope["source_time_range_utc"] == {
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-01T01:00:00Z",
    }


def test_ingests_configured_non_dallas_historical_weather_through_the_generic_operation() -> None:
    orchestrator, _, _, weather_transport = _orchestrator(
        eia_responses=[],
        weather_responses=[json_response(_weather_payload())],
    )

    result = orchestrator.ingest_open_meteo_historical(
        city=TexasCity.HOUSTON,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
    )

    assert result.dataset == "historical-weather"
    assert weather_transport.requests[0].parameters["latitude"] == str(TexasCity.HOUSTON.latitude)


def test_historical_backfill_mode_rejects_partial_weather_coverage_before_bronze_persistence() -> None:
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[],
        weather_responses=[json_response(_weather_payload())],
    )

    with pytest.raises(ContractError, match="did not cover every requested UTC hour"):
        orchestrator.ingest_open_meteo_historical(
            city=TexasCity.DALLAS,
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
            require_complete_hours=True,
        )

    assert s3.calls == []


def test_ingests_dallas_forecast_weather_using_validated_timestamps_and_units() -> None:
    orchestrator, s3, _, weather_transport = _orchestrator(
        eia_responses=[],
        weather_responses=[json_response(_weather_payload())],
    )

    result = orchestrator.ingest_open_meteo_dallas_forecast(forecast_hours=2)

    assert result.dataset == "forecast"
    assert result.record_count == 2
    assert weather_transport.requests[0].parameters["forecast_hours"] == "2"
    assert json.loads(s3.calls[0]["Body"])["units"] == _weather_payload()["hourly_units"]


def test_ingests_issued_ifs_weather_with_explicit_model_run_provenance() -> None:
    orchestrator, s3, _, weather_transport = _orchestrator(
        eia_responses=[], weather_responses=[json_response(_issued_ifs_payload())]
    )

    result = orchestrator.ingest_open_meteo_issued_ifs(
        city=TexasCity.DALLAS,
        model_run_utc=datetime(2024, 1, 1, tzinfo=timezone.utc),
        forecast_hours=2,
    )

    assert result.dataset == "issued-forecast-weather"
    assert weather_transport.requests[0].parameters["models"] == "ecmwf_ifs"
    assert weather_transport.requests[0].parameters["run"] == "2024-01-01T00:00"
    envelope = json.loads(s3.calls[0]["Body"])
    assert envelope["capture"]["request_parameters"]["models"] == "ecmwf_ifs"
    assert envelope["units"]["apparent_temperature"] == "°C"


def test_empty_eia_response_fails_by_default_without_a_bronze_write() -> None:
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[json_response({"response": {"data": []}})],
        weather_responses=[],
    )

    with pytest.raises(EmptySourceResponseError, match="allow_empty=True"):
        orchestrator.ingest_eia_ercot_demand(_query())

    assert s3.calls == []


def test_empty_eia_response_requires_explicit_opt_in_without_a_bronze_write() -> None:
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[json_response({"response": {"data": []}})],
        weather_responses=[],
    )

    assert orchestrator.ingest_eia_ercot_demand(_query(), allow_empty=True) == ()
    assert s3.calls == []


def test_provider_validation_failure_prevents_bronze_persistence() -> None:
    malformed_weather = _weather_payload()
    malformed_weather["hourly"]["time"] = []  # type: ignore[index]
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[],
        weather_responses=[json_response(malformed_weather)],
    )

    with pytest.raises(ContractError, match="must not be empty"):
        orchestrator.ingest_open_meteo_dallas_historical(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 1),
        )

    assert s3.calls == []


def test_eia_unit_derivation_failure_prevents_all_page_writes() -> None:
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[
            json_response(
                {"response": {"total": "2", "data": [_eia_record(0), _eia_record(1, unit="MW")]}}
            )
        ],
        weather_responses=[],
    )

    with pytest.raises(ContractError, match="one authoritative"):
        orchestrator.ingest_eia_ercot_demand(_query())

    assert s3.calls == []


def test_historical_eia_mode_rejects_missing_hours_before_bronze_persistence() -> None:
    orchestrator, s3, _, _ = _orchestrator(
        eia_responses=[
            json_response({"response": {"total": "2", "data": [_eia_record(0), _eia_record(2)]}})
        ],
        weather_responses=[],
    )

    with pytest.raises(ContractError, match="did not cover every requested UTC hour"):
        orchestrator.ingest_eia_ercot_demand(_query(page_size=3), require_complete_hours=True)

    assert s3.calls == []


def test_duplicate_immutable_object_is_reported_without_repeat_provider_work() -> None:
    duplicate = FakeS3ClientError(code="PreconditionFailed", status_code=412)
    orchestrator, s3, eia_transport, _ = _orchestrator(
        eia_responses=[json_response({"response": {"total": "1", "data": [_eia_record(0)]}})],
        weather_responses=[],
        s3_outcomes=[duplicate],
    )

    results = orchestrator.ingest_eia_ercot_demand(_query(page_size=1))

    assert results[0].already_exists is True
    assert len(eia_transport.requests) == 1
    assert len(s3.calls) == 1


def test_safe_bronze_failure_propagates_without_payload_disclosure() -> None:
    secret = "mock-secret-that-must-not-be-disclosed"
    orchestrator, _, _, _ = _orchestrator(
        eia_responses=[json_response({"response": {"total": "1", "data": [_eia_record(0)]}})],
        weather_responses=[],
        s3_outcomes=[RuntimeError(secret)],
    )

    with pytest.raises(BronzePersistenceError) as raised:
        orchestrator.ingest_eia_ercot_demand(_query(page_size=1))

    assert secret not in str(raised.value)


def test_three_page_eia_retry_recovers_after_a_partial_immutable_bronze_write() -> None:
    responses = [
        json_response({"response": {"total": "3", "data": [_eia_record(0)]}}),
        json_response({"response": {"total": "3", "data": [_eia_record(1)]}}),
        json_response({"response": {"total": "3", "data": [_eia_record(2)]}}),
    ]
    s3_client = RecoverableFakeS3Client(fail_on_put_number=3)
    first_attempt, first_transport = _eia_orchestrator_with_s3(responses, s3_client)

    with pytest.raises(BronzePersistenceError, match="S3 Bronze write failed"):
        first_attempt.ingest_eia_ercot_demand(_query(page_size=1))

    assert len(first_transport.requests) == 3
    assert len(s3_client.objects) == 2
    initial_objects = dict(s3_client.objects)

    retry_attempt, retry_transport = _eia_orchestrator_with_s3(responses, s3_client)
    recovered = retry_attempt.ingest_eia_ercot_demand(_query(page_size=1))

    assert len(retry_transport.requests) == 3
    assert [result.already_exists for result in recovered] == [True, True, False]
    assert len(s3_client.objects) == 3
    for key, body in initial_objects.items():
        assert s3_client.objects[key] == body
