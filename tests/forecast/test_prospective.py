from __future__ import annotations

import json
import importlib.util
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from texas_energy.forecast.prospective import (
    DEMAND_FILE,
    INPUT_MANIFEST,
    WEATHER_FILE,
    ProspectiveTrialError,
    atomic_json,
    atomic_parquet,
    sha256_file,
    validate_input_bundle,
    validate_prediction_frame,
    xgboost_feature_frame,
)
from texas_energy.spark.forecast_input import (
    ForecastInputError,
    _validate_gold_context_rows,
    _validate_issued_weather_rows,
    _put_object_if_absent,
    _require_issuable_origin,
    _validate_bundle_generation_type,
)


def _capture_script():
    path = Path(__file__).parents[2] / "scripts" / "capture_prospective_forecast_inputs.py"
    spec = importlib.util.spec_from_file_location("prospective_capture_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _runner_script():
    path = Path(__file__).parents[2] / "scripts" / "run_prospective_forecast_trial.py"
    spec = importlib.util.spec_from_file_location("prospective_runner_script", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _bundle(root: Path, *, missing_weather: bool = False) -> Path:
    issue = pd.Timestamp("2026-01-02T06:00:00Z")
    context_end = issue - pd.Timedelta(hours=24)
    hours = pd.date_range(context_end - pd.Timedelta(hours=2047), context_end, freq="h", tz="UTC")
    demand = pd.DataFrame({"period_utc": hours, "observed_average_demand_mw": range(40000, 40000 + len(hours)), "respondent": "ERCO", "demand_type": "D", "source_unit": "megawatthours"})
    rows = []
    for city in ("austin", "dallas", "el_paso", "houston", "san_antonio"):
        for valid in pd.date_range(issue + pd.Timedelta(hours=1), periods=24, freq="h", tz="UTC"):
            rows.append({"city": city, "valid_time_utc": valid, "weather_model_run_utc": issue.normalize(), "weather_available_at_utc": issue, "retrieval_time_utc": issue, "availability_basis": "test_conservative_availability", "temperature_2m_c": 20.0, "relative_humidity_pct": 50.0, "apparent_temperature_c": 19.0, "wind_speed_10m_kmh": 10.0, "temperature_2m_unit": "°C", "relative_humidity_unit": "%", "apparent_temperature_unit": "°C", "wind_speed_10m_unit": "km/h"})
    weather = pd.DataFrame(rows)
    if missing_weather: weather = weather.iloc[:-1]
    root.mkdir(); atomic_parquet(root / DEMAND_FILE, demand); atomic_parquet(root / WEATHER_FILE, weather)
    atomic_json(root / INPUT_MANIFEST, {"schema_version": "1.0", "forecast_origin_utc": issue.isoformat(), "context_end_utc": context_end.isoformat(), "demand_sha256": sha256_file(root / DEMAND_FILE), "weather_sha256": sha256_file(root / WEATHER_FILE)})
    return root


def test_bundle_requires_continuous_causal_inputs_and_builds_weather_features(tmp_path: Path) -> None:
    inputs = validate_input_bundle(_bundle(tmp_path / "bundle"))
    assert inputs.context_end_utc == pd.Timestamp("2026-01-01T06:00:00Z")
    features, selected = xgboost_feature_frame(inputs)
    assert len(features) == 24 and len(selected) == 120
    assert features["target_timestamp_utc"].iloc[0] == pd.Timestamp("2026-01-02T07:00:00Z")
    assert selected["weather_available_at_utc"].le(inputs.issue_utc).all()


def test_bundle_accepts_immutable_managed_demand_unit_compatibility(tmp_path: Path) -> None:
    """Early managed bundles retained Gold's demand_unit instead of source_unit."""

    bundle = _bundle(tmp_path / "managed-legacy")
    demand = pd.read_parquet(bundle / DEMAND_FILE).rename(columns={"source_unit": "demand_unit"})
    atomic_parquet(bundle / DEMAND_FILE, demand)
    manifest_path = bundle / INPUT_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["demand_sha256"] = sha256_file(bundle / DEMAND_FILE)
    atomic_json(manifest_path, manifest)
    assert validate_input_bundle(bundle).demand["demand_unit"].eq("megawatthours").all()


def test_bundle_rejects_incomplete_weather_and_checksum_tampering(tmp_path: Path) -> None:
    with pytest.raises(ProspectiveTrialError, match="five cities"):
        validate_input_bundle(_bundle(tmp_path / "incomplete", missing_weather=True))
    path = _bundle(tmp_path / "tampered")
    manifest = json.loads((path / INPUT_MANIFEST).read_text()); manifest["demand_sha256"] = "0" * 64
    (path / INPUT_MANIFEST).write_text(json.dumps(manifest))
    with pytest.raises(ProspectiveTrialError, match="checksum"):
        validate_input_bundle(path)


def test_aws_managed_bundle_requires_gold_as_its_demand_authority(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    manifest_path = bundle / INPUT_MANIFEST
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.update({"generation_type": "aws_managed_prospective_input", "silver_demand_uri": "s3://lake/silver/v1/demand"})
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ProspectiveTrialError, match="Gold demand source"):
        validate_input_bundle(bundle)
    manifest.pop("silver_demand_uri")
    manifest["gold_demand_features_uri"] = "s3://lake/gold/v2/demand-features"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert validate_input_bundle(bundle).manifest["gold_demand_features_uri"].endswith("demand-features")


def test_managed_historical_input_must_use_its_own_prefix() -> None:
    _validate_bundle_generation_type(
        "aws_managed_historical_replay_input",
        output_prefix="s3://lake/forecast-inputs/v1/historical-replay/",
    )
    _validate_bundle_generation_type(
        "aws_managed_prospective_input",
        output_prefix="s3://lake/forecast-inputs/v1/live/",
    )
    with pytest.raises(ForecastInputError, match="Historical replay"):
        _validate_bundle_generation_type(
            "aws_managed_historical_replay_input",
            output_prefix="s3://lake/forecast-inputs/v1/live/",
        )
    with pytest.raises(ForecastInputError, match="Prospective"):
        _validate_bundle_generation_type(
            "aws_managed_prospective_input",
            output_prefix="s3://lake/forecast-inputs/v1/historical-replay/",
        )


def test_gold_v2_context_preserves_the_model_feature_contract() -> None:
    context_end = pd.Timestamp("2026-10-02T00:00:00Z").to_pydatetime()
    context_start = context_end - pd.Timedelta(hours=2047)
    rows: list[dict[str, object]] = []
    values: dict[pd.Timestamp, float] = {}
    for hour in range(2048):
        timestamp = context_start + pd.Timedelta(hours=hour)
        value = float(50_000 + hour)
        values[pd.Timestamp(timestamp)] = value
        rows.append({
            "period_utc": timestamp, "observed_average_demand_mw": value,
            "respondent": "ERCO", "demand_type": "D", "demand_unit": "megawatthours",
            "gold_contract_version": "1", "weather_availability": "not_required_for_core_demand",
            "demand_lag_1h_mwh": value, "demand_lag_24h_mwh": value,
            "demand_lag_168h_mwh": value, "demand_trailing_mean_24h_mwh": value,
            "demand_trailing_mean_168h_mwh": value,
        })
    last = rows[-1]
    end = pd.Timestamp(context_end)
    last.update({
        "demand_lag_1h_mwh": values[end - pd.Timedelta(hours=1)],
        "demand_lag_24h_mwh": values[end - pd.Timedelta(hours=24)],
        "demand_lag_168h_mwh": values[end - pd.Timedelta(hours=168)],
        "demand_trailing_mean_24h_mwh": sum(values[end - pd.Timedelta(hours=offset)] for offset in range(1, 25)) / 24,
        "demand_trailing_mean_168h_mwh": sum(values[end - pd.Timedelta(hours=offset)] for offset in range(1, 169)) / 168,
    })
    assert _validate_gold_context_rows(rows, context_start=context_start, context_end=context_end) == {"gold_contract_version": "1"}
    last["demand_lag_24h_mwh"] = -1.0
    with pytest.raises(ForecastInputError, match="persisted feature"):
        _validate_gold_context_rows(rows, context_start=context_start, context_end=context_end)


def test_issued_ifs_rows_require_causal_availability_and_expected_units() -> None:
    issue = pd.Timestamp("2026-01-02T06:00:00Z").to_pydatetime()
    run = pd.Timestamp("2026-01-02T00:00:00Z").to_pydatetime()
    target_start = pd.Timestamp("2026-01-02T07:00:00Z").to_pydatetime()
    rows = []
    for city in ("austin", "dallas", "el_paso", "houston", "san_antonio"):
        for hour in range(24):
            rows.append({
                "city": city, "valid_time_utc": target_start + pd.Timedelta(hours=hour),
                "weather_model_run_utc": run, "weather_available_at_utc": issue,
                "retrieval_time_utc": issue, "availability_basis": "conservative_run_plus_six_hours",
                "temperature_2m_c": 20.0, "relative_humidity_pct": 50.0,
                "apparent_temperature_c": 19.0, "wind_speed_10m_kmh": 10.0,
                "temperature_2m_unit": "°C", "relative_humidity_unit": "%",
                "apparent_temperature_unit": "°C", "wind_speed_10m_unit": "km/h",
            })
    contract = _validate_issued_weather_rows(rows, issue=issue, run=run, target_start=target_start)
    assert contract["weather_availability_bases"] == ["conservative_run_plus_six_hours"]
    rows[-1]["weather_available_at_utc"] = issue + pd.Timedelta(hours=1)
    with pytest.raises(ForecastInputError, match="not available"):
        _validate_issued_weather_rows(rows, issue=issue, run=run, target_start=target_start)


def test_managed_bundle_cannot_claim_a_future_issuance_time() -> None:
    now = pd.Timestamp("2026-01-02T06:00:00Z").to_pydatetime()
    _require_issuable_origin(now, now=now)
    with pytest.raises(ForecastInputError, match="cannot be later"):
        _require_issuable_origin(now + pd.Timedelta(hours=1), now=now)


def test_glue_immutable_s3_write_uses_a_signed_conditional_header() -> None:
    class Events:
        def __init__(self) -> None:
            self.handlers: dict[str, object] = {}

        def register_first(self, name: str, handler: object, *, unique_id: str) -> None:
            self.handlers[unique_id] = handler

        def unregister(self, name: str, *, unique_id: str) -> None:
            self.handlers.pop(unique_id)

    class Client:
        def __init__(self) -> None:
            self.events = Events()
            self.meta = type("Meta", (), {"events": self.events})()
            self.kwargs: dict[str, object] | None = None
            self.headers: dict[str, object] | None = None

        def put_object(self, **kwargs: object) -> None:
            request: dict[str, object] = {"headers": {}}
            for handler in self.events.handlers.values():
                handler(params=request)
            self.kwargs, self.headers = kwargs, request["headers"]

    client = Client()
    _put_object_if_absent(client, Bucket="bucket", Key="immutable/key", Body=b"test", ServerSideEncryption="AES256")
    assert client.kwargs is not None and "IfNoneMatch" not in client.kwargs
    assert client.headers == {"If-None-Match": "*"}
    assert client.events.handlers == {}


def test_prediction_curve_cannot_use_future_feature_time(tmp_path: Path) -> None:
    inputs = validate_input_bundle(_bundle(tmp_path / "bundle"))
    targets = inputs.issue_utc + pd.to_timedelta(range(1, 25), unit="h")
    frame = pd.DataFrame({"horizon_hours": range(1, 25), "target_timestamp_utc": targets, "predicted_demand_mw": [50000.0] * 24})
    checked = validate_prediction_frame(frame, model_id="test", inputs=inputs)
    assert checked["demand_unit"].eq("MW").all()
    bad = frame.iloc[:-1]
    with pytest.raises(ProspectiveTrialError):
        validate_prediction_frame(bad, model_id="test", inputs=inputs)


def test_runner_dry_run_validates_bundle_without_writing_an_issue(tmp_path: Path) -> None:
    bundle = _bundle(tmp_path / "bundle")
    repository = Path(__file__).parents[2]
    output = tmp_path / "outputs"
    result = subprocess.run(
        [sys.executable, "scripts/run_prospective_forecast_trial.py", "--input-bundle", str(bundle), "--output-root", str(output), "--dry-run"],
        cwd=repository,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert json.loads(result.stdout)["status"] == "dry_run_validated"
    assert not output.exists()


def test_eia_key_uses_environment_before_ignored_dotenv_file(tmp_path: Path) -> None:
    script = _capture_script()
    assert script._repository_env_path() == Path(__file__).parents[2] / ".env"
    dotenv = tmp_path / ".env"
    dotenv.write_text("EIA_API_KEY=dotenv-test-key\n", encoding="utf-8")
    assert script.resolve_eia_api_key({}, env_path=dotenv) == "dotenv-test-key"
    assert script.resolve_eia_api_key({"EIA_API_KEY": "process-test-key"}, env_path=dotenv) == "process-test-key"


def test_empty_explicit_environment_does_not_fall_back_or_expose_dotenv_secret(tmp_path: Path) -> None:
    script = _capture_script()
    dotenv = tmp_path / ".env"
    secret = "do-not-expose-dotenv-test-key"
    dotenv.write_text(f"EIA_API_KEY={secret}\n", encoding="utf-8")
    with pytest.raises(RuntimeError) as error:
        script.resolve_eia_api_key({"EIA_API_KEY": ""}, env_path=dotenv)
    assert secret not in str(error.value)
    assert "not configured" in str(error.value)


def test_one_hour_eia_megawatthours_convert_to_average_megawatts() -> None:
    script = _capture_script()
    converted = script._hourly_mwh_to_average_mw(pd.Series([44394.0, 45855.0]))
    assert script.EIA_REPORTING_INTERVAL_HOURS == 1.0
    assert converted.tolist() == [44394.0, 45855.0]


def test_live_issue_keeps_issue_time_separate_from_latest_available_ifs_run() -> None:
    script = _capture_script()
    issue = pd.Timestamp("2026-10-25T14:00:00Z")
    assert script._latest_available_ifs_run(issue) == pd.Timestamp("2026-10-25T00:00:00Z")
    assert script._required_weather_forecast_hours(issue, pd.Timestamp("2026-10-25T00:00:00Z")) == 39
    assert script._origin("2026-10-25T14:00:00Z", pd.Timestamp("2026-10-25T14:01:00Z").to_pydatetime(), 2) == issue


def test_live_issue_before_current_run_availability_uses_previous_day_run() -> None:
    script = _capture_script()
    issue = pd.Timestamp("2026-10-25T05:00:00Z")
    assert script._latest_available_ifs_run(issue) == pd.Timestamp("2026-10-24T00:00:00Z")
    with pytest.raises(ValueError, match="cannot cover"):
        script._required_weather_forecast_hours(issue, pd.Timestamp("2026-10-24T00:00:00Z"))


def test_validated_bundle_publishes_create_only_objects_with_manifest_last(tmp_path: Path) -> None:
    script = _capture_script()

    class Client:
        calls: list[dict[str, object]] = []

        def put_object(self, **kwargs: object) -> dict[str, object]:
            self.calls.append(kwargs)
            return {}

    client = Client()
    published = script.publish_input_bundle_to_s3(
        _bundle(tmp_path / "bundle"),
        output_uri="s3://texas-energy-data-lake-example/forecast-inputs/v1/",
        client=client,
    )
    assert len(published) == 3
    assert [call["Key"].rsplit("/", maxsplit=1)[-1] for call in client.calls] == [DEMAND_FILE, WEATHER_FILE, INPUT_MANIFEST]
    assert all(call["IfNoneMatch"] == "*" for call in client.calls)
    assert all(call["ServerSideEncryption"] == "AES256" for call in client.calls)
    assert all(len(str(call["Metadata"]["sha256"])) == 64 for call in client.calls)


@pytest.mark.parametrize("uri", ["s3://bucket", "https://bucket/prefix/", "s3://bucket//prefix/", "s3://bucket/a/../b/"])
def test_bundle_publisher_rejects_unsafe_s3_prefix(uri: str) -> None:
    script = _capture_script()
    with pytest.raises(ValueError, match="s3 output URI"):
        script._s3_bucket_and_prefix(uri)


def test_publish_existing_requires_an_explicit_s3_destination(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    script = _capture_script()
    origin = "2026-01-02T06:00:00Z"
    issue = pd.Timestamp(origin)
    target = tmp_path / f"issue={issue.strftime('%Y%m%dT%H%M%SZ')}"
    _bundle(target)
    monkeypatch.setattr(sys, "argv", ["capture", "--input-root", str(tmp_path), "--origin-utc", origin, "--publish-existing"])
    with pytest.raises(SystemExit) as error:
        script.main()
    assert error.value.code == 2


def test_xgboost_adapter_uses_serialized_feature_order_not_context_metadata() -> None:
    script = _runner_script()

    class Booster:
        feature_names = ["context_demand_lag_1h_mwh", "weather_dallas_temperature_2m_c"]

    class Model:
        def get_booster(self):
            return Booster()

    features = pd.DataFrame(
        {
            "context_end_utc": pd.to_datetime(["2025-01-01T00:00:00Z"]),
            "context_demand_lag_1h_mwh": [50000.0],
            "weather_dallas_temperature_2m_c": [20.0],
        }
    )
    assert script._xgboost_feature_columns(Model(), features) == Booster.feature_names
    Booster.feature_names = ["missing_feature"]
    with pytest.raises(ProspectiveTrialError, match="feature contract"):
        script._xgboost_feature_columns(Model(), features)
