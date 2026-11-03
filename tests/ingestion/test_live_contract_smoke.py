"""Regression tests for sanitized failures in the opt-in live smoke script."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import importlib.util
from pathlib import Path
import sys

import pytest

from texas_energy.ingestion import EiaDemandClient, TexasCity, WeatherClient
from texas_energy.ingestion.eia import EIA_REGION_DATA_ENDPOINT, EiaDemandQuery
from texas_energy.ingestion.http import HttpRequest, HttpResponse, RetryingJsonClient, RetryPolicy

from .conftest import ScriptedTransport
from .conftest import json_response


def _weather_payload(*, start_time: datetime | None = None) -> dict[str, object]:
    start_time = start_time or datetime(2024, 1, 1, tzinfo=timezone.utc)
    start_time = start_time.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0)
    return {
        "utc_offset_seconds": 0,
        "hourly_units": {
            "temperature_2m": "°C",
            "relative_humidity_2m": "%",
            "wind_speed_10m": "km/h",
        },
        "hourly": {
            "time": [int(start_time.timestamp()), int((start_time + timedelta(hours=1)).timestamp())],
            "temperature_2m": [10.0, 11.0],
            "relative_humidity_2m": [50, 51],
            "wind_speed_10m": [12.0, 13.0],
        },
    }


@pytest.fixture(scope="module")
def smoke_script():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "verify_live_ingestion_contracts.py"
    spec = importlib.util.spec_from_file_location("live_contract_smoke_test_module", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("provider", "endpoint", "stage"),
    [
        ("EIA", "/v2/electricity/rto/region-data/data/", "ERCOT hourly demand"),
        ("Open-Meteo historical", "/v1/archive", "Dallas historical weather"),
        ("Open-Meteo forecast", "/v1/forecast", "Dallas forecast weather"),
    ],
)
def test_provider_specific_http_failures_are_sanitized(
    smoke_script, provider: str, endpoint: str, stage: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_api_key = "test-key-that-must-not-appear"
    raw_error_body = b"response-body-that-must-not-appear"
    transport = smoke_script.ReportingTransport(
        ScriptedTransport([HttpResponse(403, {}, raw_error_body)]), timeout_seconds=15.0
    )
    client = RetryingJsonClient(transport, retry_policy=RetryPolicy(max_attempts=1))

    if provider == "EIA":
        operation = lambda: smoke_script._fetch_eia(EiaDemandClient(client, api_key=fake_api_key))
    elif provider == "Open-Meteo historical":
        operation = lambda: smoke_script._fetch_historical(WeatherClient(client))
    else:
        operation = lambda: smoke_script._fetch_forecast(WeatherClient(client))

    with pytest.raises(smoke_script.SmokeVerificationError) as raised:
        smoke_script._run_target(
            provider=provider,
            endpoint={
                "EIA": smoke_script.EIA_REGION_DATA_ENDPOINT,
                "Open-Meteo historical": smoke_script.OPEN_METEO_HISTORICAL_ENDPOINT,
                "Open-Meteo forecast": smoke_script.OPEN_METEO_FORECAST_ENDPOINT,
            }[provider],
            stage=stage,
            transport=transport,
            operation=operation,
        )

    smoke_script._report_failure(raised.value)
    report = capsys.readouterr().err
    assert raised.value.status_code == 403
    assert raised.value.status_codes == (403,)
    assert f"provider: {provider}" in report
    assert f"endpoint path: {endpoint}" in report
    assert f"request stage: {stage}" in report
    assert "HTTP status: 403" in report
    assert fake_api_key not in report
    assert raw_error_body.decode("utf-8") not in report


@pytest.mark.parametrize(
    ("body", "expected_assessment"),
    [
        (b'{"error":"Invalid API key"}', "authentication rejection indicated"),
        (b'{"message":"Request blocked by WAF"}', "access restriction indicated"),
        (b"<html>forbidden</html>", "indeterminate (non-JSON response)"),
    ],
)
def test_eia_diagnostic_reports_only_safe_403_evidence(
    smoke_script, body: bytes, expected_assessment: str, capsys: pytest.CaptureFixture[str]
) -> None:
    fake_api_key = "test-key-that-must-not-appear"
    parameters = EiaDemandQuery(
        start=datetime(2024, 1, 1, tzinfo=timezone.utc),
        end=datetime(2024, 1, 1, 3, tzinfo=timezone.utc),
        page_size=4,
    ).parameters(offset=0, api_key=fake_api_key)
    transport = smoke_script.ReportingTransport(
        ScriptedTransport([HttpResponse(403, {}, body)]), timeout_seconds=15.0
    )

    response = transport.send(HttpRequest(EIA_REGION_DATA_ENDPOINT, parameters, {}, 60.0))
    assert response.status_code == 403
    smoke_script._report_eia_diagnostic(transport)

    report = capsys.readouterr().err
    assert "api_key query parameter supplied: True" in report
    assert "parameter names match contract: True" in report
    assert "UTC hour date format valid: True" in report
    assert expected_assessment in report
    assert fake_api_key not in report
    assert body.decode("utf-8") not in report


def test_eia_diagnostic_detects_safe_request_shape_mismatches(smoke_script) -> None:
    transport = smoke_script.ReportingTransport(
        ScriptedTransport([HttpResponse(403, {}, b"{}")]), timeout_seconds=15.0
    )
    malformed_parameters = {
        "api_key": "test-key-that-must-not-appear",
        "start": "not-a-date",
        "end": "2024-01-01T00",
        "unexpected": "value",
    }
    transport.send(HttpRequest(EIA_REGION_DATA_ENDPOINT, malformed_parameters, {}, 60.0))

    evidence = transport.request_evidence[0]
    assert evidence.eia_dates_are_utc_hours is False
    assert set(evidence.parameter_names) == set(malformed_parameters)


def test_ssm_credential_source_delegates_without_exposing_the_secret(
    smoke_script, monkeypatch: pytest.MonkeyPatch
) -> None:
    calls: list[dict[str, str | None]] = []

    def fake_provider(*, profile_name: str | None, region_name: str) -> str:
        calls.append({"profile_name": profile_name, "region_name": region_name})
        return "test-only-key"

    monkeypatch.setattr(smoke_script, "get_eia_api_key_from_ssm", fake_provider)

    assert smoke_script._resolve_eia_api_key(
        credential_source="ssm",
        aws_profile="texas-energy",
        aws_region="us-east-1",
    ) == "test-only-key"
    assert calls == [{"profile_name": "texas-energy", "region_name": "us-east-1"}]


def test_environment_credential_source_remains_available_for_existing_local_tests(
    smoke_script, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("EIA_API_KEY", "test-only-key")

    assert smoke_script._resolve_eia_api_key(
        credential_source="environment",
        aws_profile=None,
        aws_region="us-east-1",
    ) == "test-only-key"


def test_open_meteo_only_mode_runs_both_operations_without_eia_credentials(
    smoke_script, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    current_time = datetime.now(timezone.utc)
    historical_start = datetime.combine(
        (current_time - timedelta(days=7)).date(), datetime.min.time(), tzinfo=timezone.utc
    )
    historical_transport = smoke_script.ReportingTransport(
        ScriptedTransport([json_response(_weather_payload(start_time=historical_start))]), timeout_seconds=15.0
    )
    forecast_transport = smoke_script.ReportingTransport(
        ScriptedTransport([json_response(_weather_payload(start_time=current_time))]), timeout_seconds=15.0
    )

    def fail_if_eia_credential_is_resolved(**_kwargs: object) -> str:
        pytest.fail("Open-Meteo-only mode must not resolve an EIA credential.")

    monkeypatch.setattr(smoke_script, "_resolve_eia_api_key", fail_if_eia_credential_is_resolved)
    transports = iter((historical_transport, forecast_transport))
    monkeypatch.setattr(smoke_script, "_new_transport", lambda: next(transports))
    monkeypatch.setattr(sys, "argv", ["verify_live_ingestion_contracts.py", "--live", "--open-meteo-only"])

    exit_code = smoke_script.main()

    report = capsys.readouterr().out
    assert exit_code == 0
    assert "Open-Meteo historical" in report
    assert "Open-Meteo forecast" in report
    assert "HTTP outcome: 200" in report
    assert "UTC range:" in report
    assert "units:" in report
    assert "contract validation: passed" in report
    assert len(historical_transport.request_evidence) == 1
    assert len(forecast_transport.request_evidence) == 1


@pytest.mark.parametrize(
    ("failed_operation", "expected_provider", "expected_stage"),
    [
        ("historical", "Open-Meteo historical", "Dallas historical weather"),
        ("forecast", "Open-Meteo forecast", "Dallas forecast weather"),
    ],
)
def test_open_meteo_only_mode_identifies_the_failed_operation(
    smoke_script,
    failed_operation: str,
    expected_provider: str,
    expected_stage: str,
    capsys: pytest.CaptureFixture[str],
) -> None:
    current_time = datetime.now(timezone.utc)
    historical_start = datetime.combine(
        (current_time - timedelta(days=7)).date(), datetime.min.time(), tzinfo=timezone.utc
    )
    historical_response = (
        HttpResponse(503, {}, b"service-error")
        if failed_operation == "historical"
        else json_response(_weather_payload(start_time=historical_start))
    )
    forecast_response = (
        HttpResponse(503, {}, b"service-error")
        if failed_operation == "forecast"
        else json_response(_weather_payload(start_time=current_time))
    )
    historical_transport = smoke_script.ReportingTransport(
        ScriptedTransport([historical_response, historical_response]), timeout_seconds=15.0
    )
    forecast_transport = smoke_script.ReportingTransport(
        ScriptedTransport([forecast_response, forecast_response]), timeout_seconds=15.0
    )

    exit_code = smoke_script._run_open_meteo_only(
        historical_transport=historical_transport,
        forecast_transport=forecast_transport,
    )

    report = capsys.readouterr().err
    assert exit_code == 1
    assert f"provider: {expected_provider}" in report
    assert f"request stage: {expected_stage}" in report
    assert "HTTP status: 503" in report
