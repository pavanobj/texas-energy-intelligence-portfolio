"""State-transition contracts for normal and model-only forecast execution.

These tests deliberately mock synchronous service responses.  They verify that
the ASL result paths preserve the original execution input without paying for a
second Glue run or SageMaker job.
"""

from __future__ import annotations

from copy import deepcopy
from pathlib import Path
import re


PIPELINE = (
    Path(__file__).parents[1] / "infra" / "terraform" / "app" / "production_pipeline.tf"
).read_text(encoding="utf-8")
VARIABLES = (
    Path(__file__).parents[1] / "infra" / "terraform" / "app" / "variables.tf"
).read_text(encoding="utf-8")


def _put_result(execution: dict[str, object], result_path: str, response: object) -> dict[str, object]:
    """Small Step Functions ResultPath mock for the top-level paths we use."""

    assert result_path.startswith("$.")
    merged = deepcopy(execution)
    merged[result_path[2:]] = response
    return merged


def _captured_issue() -> dict[str, object]:
    return {
        "forecast_issue_id": "20261026T150000Z",
        "forecast_origin_utc": "2026-10-26T15:00:00Z",
        "weather_model_run_utc": "2026-10-26T12:00:00Z",
        "demand_start_utc": "2026-10-19T15:00:00Z",
        "demand_end_utc": "2026-10-26T15:00:00Z",
        "weather_start_date": "2026-10-19",
        "weather_end_date": "2026-10-26",
    }


def test_normal_path_preserves_input_through_ingestion_glue_and_all_models() -> None:
    # model_only is absent, so ChooseExecutionMode defaults to IngestDemand.
    assert 'StartAt = "ChooseExecutionMode"' in PIPELINE
    assert 'Default = "IngestDemand"' in PIPELINE
    for edge in (
        'Next           = "ValidateDemandIngestion"',
        'Next  = "ValidateHistoricalWeatherIngestion"',
        'Next           = "ValidateForecastWeatherIngestion"',
        'Next = "TransformAndPrepareForecastInput"',
        'Next = "RunBiLSTM"',
        'Next = "RunChronos2"',
        'Next = "RefreshDashboardEvaluation"',
    ):
        assert edge in PIPELINE

    state = _captured_issue()
    for result_path, mocked_response in (
        ("$.demand", {"Payload": {"status": "completed"}}),
        ("$.historical_weather", {"Payload": {"status": "completed"}}),
        ("$.forecast_weather", {"Payload": {"status": "completed"}}),
        ("$.glue_forecast_input", {"JobRunState": "SUCCEEDED"}),
        ("$.xgboost_processing", {"ProcessingJobStatus": "Completed"}),
        ("$.bilstm_processing", {"ProcessingJobStatus": "Completed"}),
        ("$.chronos2_processing", {"ProcessingJobStatus": "Completed"}),
        ("$.dashboard_evaluation", {"JobRunState": "SUCCEEDED"}),
    ):
        assert f'ResultPath = "{result_path}"' in PIPELINE
        state = _put_result(state, result_path, mocked_response)
        assert state["forecast_issue_id"] == "20261026T150000Z"
        assert state["forecast_origin_utc"] == "2026-10-26T15:00:00Z"
        assert state["weather_model_run_utc"] == "2026-10-26T12:00:00Z"

    assert state["chronos2_processing"] == {"ProcessingJobStatus": "Completed"}
    assert state["dashboard_evaluation"] == {"JobRunState": "SUCCEEDED"}
    assert '"--dashboard-evaluation-only" = "true"' in PIPELINE
    assert 'ResultPath = "$.dashboard_evaluation_failure"' in PIPELINE


def test_model_only_recovery_requires_captured_issue_and_runs_all_models() -> None:
    assert re.search(r'Variable\s*=\s*"\$\.model_only"', PIPELINE)
    assert re.search(r'Next\s*=\s*"ValidateModelOnlyContract"', PIPELINE)
    assert 'Default = "ForecastInputContractInvalid"' in PIPELINE
    for field in ("forecast_origin_utc", "weather_model_run_utc", "forecast_issue_id"):
        assert f'Variable = "$.{field}", IsPresent = true' in PIPELINE

    state = _captured_issue() | {"model_only": True}
    for result_path, mocked_response in (
        ("$.xgboost_processing", {"ProcessingJobStatus": "Completed"}),
        ("$.bilstm_processing", {"ProcessingJobStatus": "Completed"}),
        ("$.chronos2_processing", {"ProcessingJobStatus": "Completed"}),
        ("$.dashboard_evaluation", {"JobRunState": "SUCCEEDED"}),
    ):
        state = _put_result(state, result_path, mocked_response)
        assert state["forecast_issue_id"] == "20261026T150000Z"

    for model in ("xgboost", "bilstm", "chronos2"):
        assert f"States.Format('te-{model}-{{}}-{{}}', $.forecast_issue_id, $$.Execution.Name)" in PIPELINE


def test_approved_selection_is_opt_in_and_keeps_legacy_champions_as_default() -> None:
    assert 'variable "enable_approved_model_selection"' in VARIABLES
    assert "default     = false" in VARIABLES
    assert "models/v1/xgboost/final-closeout-653-20261019/" in PIPELINE
    assert "models/v1/bilstm/final-closeout-653-20261019/" in PIPELINE
    assert PIPELINE.count("--approved-model-selection") == 2
    chronos_state = PIPELINE.split("RunChronos2 = {", maxsplit=1)[1].split("RefreshDashboardEvaluation", maxsplit=1)[0]
    assert "--approved-model-selection" not in chronos_state
