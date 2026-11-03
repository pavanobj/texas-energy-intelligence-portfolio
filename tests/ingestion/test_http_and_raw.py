from __future__ import annotations

from datetime import datetime, timezone
import signal
import socket
import time
import traceback
from urllib.error import URLError

import pytest

from texas_energy.ingestion.errors import ContractError, ResponseDecodeError, TransportError
from texas_energy.ingestion.http import HttpRequest, HttpResponse, RetryingJsonClient, RetryPolicy, UrllibTransport
from texas_energy.ingestion.raw import RawCapture, request_fingerprint

from .conftest import ScriptedTransport, json_response


def test_rate_limit_retries_using_retry_after_without_network_access() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(429, {"Retry-After": "2"}, b'{"error":"slow down"}'),
            json_response({"ok": True}),
        ]
    )
    sleeps: list[float] = []
    client = RetryingJsonClient(
        transport,
        retry_policy=RetryPolicy(max_attempts=2),
        sleep=sleeps.append,
    )

    response = client.get_json("https://example.invalid/source", {"scope": "test"})

    assert response.payload == {"ok": True}
    assert sleeps == [2.0]
    assert len(transport.requests) == 2


def test_per_provider_rate_limits_gate_every_request_without_network_access() -> None:
    transport = ScriptedTransport([json_response({"ok": 1}), json_response({"ok": 2}), json_response({"ok": 3})])
    clock = [0.0]
    sleeps: list[float] = []

    def sleep(seconds: float) -> None:
        sleeps.append(seconds)
        clock[0] += seconds

    client = RetryingJsonClient(
        transport,
        sleep=sleep,
        monotonic=lambda: clock[0],
        minimum_interval_seconds_by_host={"api.eia.gov": 1.0, "archive-api.open-meteo.com": 0.25},
    )

    client.get_json("https://api.eia.gov/v2/example", {})
    client.get_json("https://api.eia.gov/v2/example", {})
    client.get_json("https://archive-api.open-meteo.com/v1/archive", {})

    assert sleeps == [1.0]
    assert len(transport.requests) == 3


def test_invalid_json_is_a_contract_error() -> None:
    secret = "raw-payload-secret-that-must-not-appear"
    client = RetryingJsonClient(
        ScriptedTransport([HttpResponse(200, {}, f'{{"api_key":"{secret}",}}'.encode("utf-8"))])
    )

    with pytest.raises(ResponseDecodeError) as raised:
        client.get_json("https://example.invalid/source", {})

    assert secret not in str(raised.value)
    assert secret not in "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )


def test_server_errors_and_transport_timeouts_retry_with_bounded_attempts() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(503, {}, b"unavailable"),
            TransportError("simulated timeout"),
            json_response({"ok": True}),
        ]
    )
    sleeps: list[float] = []
    client = RetryingJsonClient(
        transport,
        retry_policy=RetryPolicy(max_attempts=3, base_delay_seconds=0.25),
        sleep=sleeps.append,
    )

    assert client.get_json("https://example.invalid/source", {}).payload == {"ok": True}
    assert sleeps == [0.25, 0.5]


def test_retry_after_http_date_is_calculated_from_injected_time() -> None:
    transport = ScriptedTransport(
        [
            HttpResponse(429, {"Retry-After": "Mon, 01 Jan 2024 00:00:30 GMT"}, b"rate limited"),
            json_response({"ok": True}),
        ]
    )
    sleeps: list[float] = []
    client = RetryingJsonClient(
        transport,
        retry_policy=RetryPolicy(max_attempts=2),
        sleep=sleeps.append,
        now=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    assert client.get_json("https://example.invalid/source", {}).payload == {"ok": True}
    assert sleeps == [30.0]


def test_retry_after_over_execution_budget_fails_without_retrying_early() -> None:
    client = RetryingJsonClient(
        ScriptedTransport([HttpResponse(429, {"Retry-After": "60"}, b"rate limited")]),
        retry_policy=RetryPolicy(max_attempts=2, max_retry_after_seconds=10),
        sleep=lambda _: pytest.fail("must not retry before Retry-After"),
    )

    with pytest.raises(TransportError, match="execution budget"):
        client.get_json("https://example.invalid/source", {})


def test_urllib_transport_normalizes_native_timeout_without_opening_a_socket(monkeypatch: pytest.MonkeyPatch) -> None:
    def raise_timeout(*args: object, **kwargs: object) -> object:
        raise TimeoutError("simulated timeout")

    monkeypatch.setattr("texas_energy.ingestion.http.urlopen", raise_timeout)

    with pytest.raises(TransportError):
        UrllibTransport().send(HttpRequest("https://example.invalid/source", {}, {}, 1.0))


def test_urllib_transport_traceback_hides_a_credential_bearing_native_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    secret = "url-secret-that-must-not-appear"

    def raise_url_error(*args: object, **kwargs: object) -> object:
        raise URLError(f"https://example.invalid/source?api_key={secret}")

    monkeypatch.setattr("texas_energy.ingestion.http.urlopen", raise_url_error)

    with pytest.raises(TransportError) as raised:
        UrllibTransport().send(
            HttpRequest("https://example.invalid/source", {"api_key": secret}, {}, 1.0)
        )

    assert secret not in str(raised.value)
    assert secret not in "".join(
        traceback.format_exception(type(raised.value), raised.value, raised.value.__traceback__)
    )


def test_urllib_transport_classifies_dns_without_retaining_native_reason(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def raise_dns_error(*args: object, **kwargs: object) -> object:
        raise URLError(socket.gaierror("secret-provider-host.example"))

    monkeypatch.setattr("texas_energy.ingestion.http.urlopen", raise_dns_error)

    with pytest.raises(TransportError) as raised:
        UrllibTransport().send(HttpRequest("https://example.invalid/source", {}, {}, 1.0))

    assert raised.value.phase == "dns"
    assert "secret-provider-host" not in str(raised.value)


def test_urllib_transport_rejects_an_oversized_response_body_before_json_parsing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeResponse:
        status = 200
        headers: dict[str, str] = {}

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: object) -> None:
            return None

        def read(self, size: int) -> bytes:
            return b"x" * size

    monkeypatch.setattr("texas_energy.ingestion.http.urlopen", lambda *args, **kwargs: FakeResponse())

    with pytest.raises(TransportError, match="size limit"):
        UrllibTransport(max_response_bytes=32).send(HttpRequest("https://example.invalid/source", {}, {}, 1.0))


def test_urllib_transport_rejects_an_invalid_hard_deadline() -> None:
    with pytest.raises(ValueError, match="hard_deadline_seconds"):
        UrllibTransport(hard_deadline_seconds=0)


@pytest.mark.skipif(not hasattr(signal, "SIGALRM"), reason="SIGALRM deadline is Linux-only")
def test_urllib_transport_hard_deadline_bounds_a_stalled_open(monkeypatch: pytest.MonkeyPatch) -> None:
    def stall(*args: object, **kwargs: object) -> object:
        time.sleep(1.0)
        raise AssertionError("the hard deadline should interrupt this wait")

    monkeypatch.setattr("texas_energy.ingestion.http.urlopen", stall)

    started = time.monotonic()
    with pytest.raises(TransportError, match="bounded open deadline"):
        UrllibTransport(hard_deadline_seconds=0.05).send(
            HttpRequest("https://example.invalid/source", {}, {}, 30.0)
        )
    assert time.monotonic() - started < 0.5


def test_fingerprint_and_manifest_redact_credentials_deterministically() -> None:
    first_key = "test-api-key-alpha"
    second_key = "test-api-key-beta"
    first = request_fingerprint(
        "https://example.invalid/source",
        {"api_key": first_key, "scope": "ERCO"},
        {"Authorization": f"Bearer {first_key}"},
    )
    second = request_fingerprint(
        "https://example.invalid/source",
        {"scope": "ERCO", "api_key": second_key},
        {"Authorization": f"Bearer {second_key}"},
    )
    capture = RawCapture(
        source="example",
        dataset="test",
        endpoint="https://example.invalid/source",
        request_parameters={"api_key": first_key, "scope": "ERCO", "access_token": second_key},
        raw_payload=b'{"data":[]}',
        retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
        status_code=200,
        response_headers={"Content-Type": "application/json", "Set-Cookie": second_key},
    )

    assert first == second
    manifest = capture.manifest()
    assert manifest["request_parameters"] == {
        "api_key": "[REDACTED]",
        "scope": "ERCO",
        "access_token": "[REDACTED]",
    }
    serialized_metadata = capture.manifest_bytes().decode("utf-8")
    for fake_credential in (first_key, second_key):
        assert fake_credential not in repr(capture)
        assert fake_credential not in serialized_metadata
        assert fake_credential not in capture.fingerprint


def test_capture_metadata_rejects_endpoint_queries_and_fragments() -> None:
    with pytest.raises(ContractError, match="query strings or fragments"):
        request_fingerprint("https://example.invalid/source?api_key=test", {})

    with pytest.raises(ContractError, match="query strings or fragments"):
        RawCapture(
            source="example",
            dataset="test",
            endpoint="https://example.invalid/source#token=test",
            request_parameters={},
            raw_payload=b"{}",
            retrieved_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
            status_code=200,
            response_headers={},
        )
