"""Mock-only tests for generic, secret-safe Bronze S3 persistence."""

from __future__ import annotations

from datetime import date, datetime, timezone
import json
import traceback

import pytest

from texas_energy.ingestion import bronze as bronze_module
from texas_energy.ingestion.bronze import BronzeS3Writer, BronzeSourceTimeRange, bronze_envelope
from texas_energy.ingestion.eia import EiaDemandClient, EiaDemandQuery
from texas_energy.ingestion.errors import BronzePersistenceError
from texas_energy.ingestion.http import RetryingJsonClient
from texas_energy.ingestion.open_meteo import TexasCity, WeatherClient
from texas_energy.ingestion.raw import RawCapture

from .conftest import ScriptedTransport, json_response


class FakeS3Client:
    def __init__(self, outcomes: list[Exception | None] | None = None) -> None:
        self.outcomes = list(outcomes or [])
        self.calls: list[dict[str, object]] = []

    def put_object(self, **kwargs: object) -> dict[str, object]:
        self.calls.append(kwargs)
        if self.outcomes:
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome
        return {"ETag": '"test"'}


class FakeS3ClientError(Exception):
    def __init__(self, *, code: str, status_code: int, message: str = "mock S3 failure") -> None:
        super().__init__(message)
        self.response = {
            "Error": {"Code": code, "Message": message},
            "ResponseMetadata": {"HTTPStatusCode": status_code},
        }


def _capture(*, payload: bytes = b'{"records":[1]}', retrieved_at: datetime | None = None) -> RawCapture:
    return RawCapture(
        source="eia",
        dataset="ercot_demand",
        endpoint="https://api.eia.gov/v2/electricity/rto/region-data/data/",
        request_parameters={"api_key": "secret-api-key", "respondent": "ERCO"},
        raw_payload=payload,
        retrieved_at=retrieved_at or datetime(2024, 1, 2, 3, tzinfo=timezone.utc),
        status_code=200,
        response_headers={"Content-Type": "application/json", "Authorization": "Bearer secret-api-key"},
    )


def _source_time_range() -> BronzeSourceTimeRange:
    return BronzeSourceTimeRange(
        start=datetime(2024, 1, 1, 23, tzinfo=timezone.utc),
        end=datetime(2024, 1, 2, 2, tzinfo=timezone.utc),
    )


def test_writes_a_versioned_sanitized_envelope_to_a_deterministic_key() -> None:
    client = FakeS3Client()
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    result = writer.write(
        _capture(),
        source_time_range=_source_time_range(),
        units={"demand": "megawatthours"},
    )

    assert result.bucket == "texas-energy-bronze-example"
    assert result.key.startswith("bronze/v1/source=eia/dataset=ercot_demand/data_date=2024-01-01/request=")
    assert result.key.endswith(".json")
    assert len(client.calls) == 1
    call = client.calls[0]
    assert call["Bucket"] == result.bucket
    assert call["Key"] == result.key
    assert call["ContentType"] == "application/json"
    assert call["ServerSideEncryption"] == "AES256"
    assert call["IfNoneMatch"] == "*"
    assert result.already_exists is False
    body = json.loads(call["Body"])
    assert body["schema_version"] == "1.1"
    assert body["source_time_range_utc"] == {"start": "2024-01-01T23:00:00Z", "end": "2024-01-02T02:00:00Z"}
    assert body["units"] == {"demand": "megawatthours"}
    assert body["source_payload"] == {"records": [1]}
    assert body["sanitized_payload_sha256"]


def test_identical_capture_retries_target_the_same_key() -> None:
    client = FakeS3Client()
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")
    capture = _capture()

    first = writer.write(capture, source_time_range=_source_time_range())
    second = writer.write(capture, source_time_range=_source_time_range())

    assert first.key == second.key
    assert first.already_exists is False
    assert second.already_exists is False
    assert client.calls[0]["Key"] == client.calls[1]["Key"]
    assert client.calls[0]["Body"] == client.calls[1]["Body"]


def test_changed_payload_has_a_distinct_immutable_target_with_the_same_request_identity() -> None:
    writer = BronzeS3Writer(FakeS3Client(), bucket_name="texas-energy-bronze-example")

    first = writer.object_key(_capture(payload=b'{"records":[1]}'), _source_time_range())
    second = writer.object_key(_capture(payload=b'{"records":[2]}'), _source_time_range())

    assert "/request=" + _capture().fingerprint in first
    assert "/request=" + _capture().fingerprint in second
    assert first != second
    assert "/raw_payload=" in first


def test_precondition_failed_reports_an_existing_immutable_object_without_a_read() -> None:
    client = FakeS3Client([FakeS3ClientError(code="PreconditionFailed", status_code=412)])
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    result = writer.write(_capture(), source_time_range=_source_time_range())

    assert result.already_exists is True
    assert len(client.calls) == 1
    assert client.calls[0]["IfNoneMatch"] == "*"


def test_conditional_request_conflict_retries_then_writes() -> None:
    client = FakeS3Client([FakeS3ClientError(code="ConditionalRequestConflict", status_code=409), None])
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    result = writer.write(_capture(), source_time_range=_source_time_range())

    assert result.already_exists is False
    assert len(client.calls) == 2


def test_conditional_request_conflict_fails_after_the_bounded_retry_limit() -> None:
    secret = "test-secret-that-must-not-be-disclosed"
    client = FakeS3Client(
        [
            FakeS3ClientError(code="ConditionalRequestConflict", status_code=409, message=secret),
            FakeS3ClientError(code="ConditionalRequestConflict", status_code=409, message=secret),
        ]
    )
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    with pytest.raises(BronzePersistenceError) as raised:
        writer.write(_capture(), source_time_range=_source_time_range())

    assert "conditional write conflicted" in str(raised.value)
    assert secret not in str(raised.value)
    assert len(client.calls) == 2


def test_raw_and_sanitized_payload_hashes_distinguish_byte_and_json_identities() -> None:
    compact = _capture(payload=b'{"records":[1]}')
    pretty = _capture(payload=b'{\n  "records": [1]\n}')
    writer = BronzeS3Writer(FakeS3Client(), bucket_name="texas-energy-bronze-example")

    compact_envelope = bronze_envelope(compact, source_time_range=_source_time_range())
    pretty_envelope = bronze_envelope(pretty, source_time_range=_source_time_range())

    assert compact.payload_sha256 != pretty.payload_sha256
    assert compact_envelope["sanitized_payload_sha256"] == pretty_envelope["sanitized_payload_sha256"]
    assert writer.object_key(compact, _source_time_range()) != writer.object_key(pretty, _source_time_range())


def test_object_key_is_stable_when_only_ingestion_timestamp_changes() -> None:
    writer = BronzeS3Writer(FakeS3Client(), bucket_name="texas-energy-bronze-example")
    first = _capture(retrieved_at=datetime(2024, 1, 2, 3, tzinfo=timezone.utc))
    second = _capture(retrieved_at=datetime(2024, 2, 2, 3, tzinfo=timezone.utc))

    assert writer.object_key(first, _source_time_range()) == writer.object_key(second, _source_time_range())


@pytest.mark.parametrize(
    "bucket_name",
    ["ab", "Texas-Energy", "192.168.1.1", "bad..bucket", "bad_bucket"],
)
def test_rejects_invalid_bucket_names(bucket_name: str) -> None:
    with pytest.raises(ValueError, match="S3 bucket names"):
        BronzeS3Writer(FakeS3Client(), bucket_name=bucket_name)


@pytest.mark.parametrize("prefix", ["", "/bronze", "bronze/", "bronze//raw", "bronze/unsafe value"])
def test_rejects_invalid_prefixes(prefix: str) -> None:
    with pytest.raises(ValueError):
        BronzeS3Writer(FakeS3Client(), bucket_name="texas-energy-bronze-example", prefix=prefix)


def test_rejects_invalid_capture_partition_values_and_oversized_envelopes() -> None:
    invalid_capture = RawCapture(
        source="eia\x00source",
        dataset="ercot_demand",
        endpoint="https://example.invalid/data",
        request_parameters={},
        raw_payload=b"{}",
        retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        status_code=200,
        response_headers={},
    )
    writer = BronzeS3Writer(FakeS3Client(), bucket_name="texas-energy-bronze-example")
    with pytest.raises(ValueError, match="Bronze source"):
        writer.object_key(invalid_capture, _source_time_range())

    too_small_writer = BronzeS3Writer(
        FakeS3Client(),
        bucket_name="texas-energy-bronze-example",
        max_object_bytes=10,
    )
    with pytest.raises(BronzePersistenceError, match="object-size limit"):
        too_small_writer.write(_capture(), source_time_range=_source_time_range())

    with pytest.raises(BronzePersistenceError, match="payload exceeds"):
        too_small_writer.write(
            _capture(payload=b'{"records":["this-is-larger-than-ten-bytes"]}'),
            source_time_range=_source_time_range(),
        )

    long_prefix = "/".join(["a" * 128] * 8)
    long_key_writer = BronzeS3Writer(
        FakeS3Client(),
        bucket_name="texas-energy-bronze-example",
        prefix=long_prefix,
    )
    with pytest.raises(ValueError, match="object keys"):
        long_key_writer.object_key(_capture(), _source_time_range())


def test_rejects_invalid_payload_serialization_without_writing() -> None:
    client = FakeS3Client()
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    with pytest.raises(BronzePersistenceError, match="valid UTF-8 JSON"):
        writer.write(_capture(payload=b"not-json"), source_time_range=_source_time_range())

    assert client.calls == []


def test_reports_envelope_serialization_failures_without_writing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = FakeS3Client()
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    def fail_serialization(_value: object) -> bytes:
        raise TypeError("test-only serialization failure")

    monkeypatch.setattr(bronze_module, "canonical_json_bytes", fail_serialization)

    with pytest.raises(BronzePersistenceError, match="could not be serialized"):
        writer.write(_capture(), source_time_range=_source_time_range())

    assert client.calls == []


def test_sanitizes_payload_secrets_and_credential_bearing_urls_before_persistence() -> None:
    secret = "test-secret-that-must-not-be-persisted"
    payload = json.dumps(
        {
            "api_key": secret,
            "endpoint": f"https://example.invalid/data?api_key={secret}",
            "query_string": f"api_key={secret}&respondent=ERCO",
            "nested": {"authorization": f"Bearer {secret}"},
            "records": [1],
        }
    ).encode("utf-8")
    client = FakeS3Client()
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    writer.write(_capture(payload=payload), source_time_range=_source_time_range())

    persisted = client.calls[0]["Body"].decode("utf-8")
    assert secret not in persisted
    envelope = json.loads(persisted)
    assert envelope["source_payload"]["api_key"] == "[REDACTED]"
    assert envelope["source_payload"]["endpoint"] == "[REDACTED]"
    assert envelope["source_payload"]["query_string"] == "[REDACTED]"
    assert envelope["source_payload"]["nested"]["authorization"] == "[REDACTED]"


def test_s3_failures_are_safe_and_actionable_without_payload_disclosure() -> None:
    secret = "test-secret-that-must-not-be-disclosed"
    client = FakeS3Client([RuntimeError(secret)])
    writer = BronzeS3Writer(client, bucket_name="texas-energy-bronze-example")

    with pytest.raises(BronzePersistenceError) as raised:
        writer.write(_capture(payload=json.dumps({"api_key": secret}).encode("utf-8")), source_time_range=_source_time_range())

    assert "S3 Bronze write failed" in str(raised.value)
    assert secret not in str(raised.value)
    assert secret not in "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )


def test_envelope_requires_a_valid_aware_source_time_range_and_safe_units() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        BronzeSourceTimeRange(start=datetime(2024, 1, 1), end=datetime(2024, 1, 1, 1))
    with pytest.raises(ValueError, match="end must not precede"):
        BronzeSourceTimeRange(
            start=datetime(2024, 1, 2, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, tzinfo=timezone.utc),
        )
    with pytest.raises(ValueError, match="unit field"):
        bronze_envelope(_capture(), source_time_range=_source_time_range(), units={"bad field": "MW"})
    with pytest.raises(ValueError, match="credential-bearing"):
        bronze_envelope(_capture(), source_time_range=_source_time_range(), units={"api_key": "secret"})


def test_writes_a_realistic_eia_client_capture_without_provider_specific_writer_logic() -> None:
    eia_payload = {
        "response": {
            "total": "1",
            "data": [
                {
                    "period": "2024-01-01T00",
                    "respondent": "ERCO",
                    "respondent-name": "Electric Reliability Council of Texas, Inc.",
                    "type": "D",
                    "type-name": "Demand",
                    "value": "45000",
                    "value-units": "megawatthours",
                }
            ],
        }
    }
    client = EiaDemandClient(
        RetryingJsonClient(ScriptedTransport([json_response(eia_payload)])),
        api_key="mock-api-key",
        retrieved_at=lambda: datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
    )
    fetched = client.fetch(
        EiaDemandQuery(
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, tzinfo=timezone.utc),
            page_size=1,
        )
    )
    bronze_client = FakeS3Client()
    writer = BronzeS3Writer(bronze_client, bucket_name="texas-energy-bronze-example")

    writer.write(
        fetched.pages[0].capture,
        source_time_range=BronzeSourceTimeRange(
            start=datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        units={"value": "megawatthours"},
    )

    envelope = json.loads(bronze_client.calls[0]["Body"])
    record = envelope["source_payload"]["response"]["data"][0]
    assert record["period"] == "2024-01-01T00"
    assert record["value-units"] == "megawatthours"
    assert envelope["units"] == {"value": "megawatthours"}
    assert envelope["source_time_range_utc"] == {
        "start": "2024-01-01T00:00:00Z",
        "end": "2024-01-01T00:00:00Z",
    }
    assert envelope["capture"]["request_parameters"]["api_key"] == "[REDACTED]"


def test_writes_a_realistic_open_meteo_client_capture_without_provider_specific_writer_logic() -> None:
    weather_payload = {
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
    client = WeatherClient(
        RetryingJsonClient(ScriptedTransport([json_response(weather_payload)])),
        retrieved_at=lambda: datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
    )
    fetched = client.fetch_historical(
        TexasCity.DALLAS,
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 1),
    )
    bronze_client = FakeS3Client()
    writer = BronzeS3Writer(bronze_client, bucket_name="texas-energy-bronze-example")

    writer.write(
        fetched.capture,
        source_time_range=BronzeSourceTimeRange(
            start=fetched.hourly_times_utc[0],
            end=fetched.hourly_times_utc[-1],
        ),
        units=weather_payload["hourly_units"],
    )

    envelope = json.loads(bronze_client.calls[0]["Body"])
    hourly = envelope["source_payload"]["hourly"]
    assert hourly["time"] == [1_704_067_200, 1_704_070_800]
    assert hourly["temperature_2m"] == [10.0, 11.0]
    assert hourly["relative_humidity_2m"] == [50, 51]
    assert hourly["wind_speed_10m"] == [12.0, 13.0]
    assert envelope["source_payload"]["hourly_units"] == weather_payload["hourly_units"]
    assert envelope["units"] == weather_payload["hourly_units"]
