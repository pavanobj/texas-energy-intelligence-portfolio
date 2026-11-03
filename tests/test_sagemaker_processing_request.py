import json
import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "build_sagemaker_processing_request",
    Path(__file__).parents[1] / "scripts" / "build_sagemaker_processing_request.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
ProcessingRequestError = _MODULE.ProcessingRequestError
build_request = _MODULE.build_request


CONFIG = Path("config/sagemaker/jobs.json")


def test_builds_immutable_processing_request_for_each_model() -> None:
    for model in ("xgboost", "bilstm", "chronos2"):
        request = build_request(CONFIG, model=model, job_name=f"te-{model}-validation-1", loader_revision="a" * 40)
        assert request["AppSpecification"]["ImageUri"].endswith("b89aa56")
        assert "ContainerEntrypoint" not in request["AppSpecification"]
        assert request["AppSpecification"]["ContainerArguments"][:2] == ["--model", model]
        assert request["ProcessingInputs"][0]["S3Input"]["S3Uri"].endswith("issue=20251230T060000Z/")
        assert request["ProcessingInputs"][1]["S3Input"]["S3Uri"].startswith("s3://texas-energy-data-lake-example/models/v1/")
        assert request["ProcessingOutputConfig"]["Outputs"][0]["S3Output"]["S3Uri"].endswith(f"/{model}/run=te-{model}-validation-1/")
        assert request["StoppingCondition"]["MaxRuntimeInSeconds"] == 3600


def test_rejects_non_immutable_job_name_and_image(tmp_path: Path) -> None:
    with pytest.raises(ProcessingRequestError, match="job name"):
        build_request(CONFIG, model="xgboost", job_name="not/valid", loader_revision="a")
    payload = json.loads(CONFIG.read_text())
    payload["managed_execution"]["image_uri"] = "tag-only"
    changed = tmp_path / "jobs.json"; changed.write_text(json.dumps(payload))
    with pytest.raises(ProcessingRequestError, match="immutable"):
        build_request(changed, model="xgboost", job_name="valid-name", loader_revision="a")


def test_builds_live_request_from_explicit_immutable_bundle() -> None:
    request = build_request(
        CONFIG,
        model="chronos2",
        job_name="te-chronos2-live-20261025t2300z",
        loader_revision="b" * 40,
        input_uri="s3://texas-energy-data-lake-example/forecast-inputs/v1/issue=20261025T230000Z/",
        output_prefix="s3://texas-energy-data-lake-example/forecasts/v1/live/issue=20261025T230000Z/",
        generation_type="prospective_inference",
    )
    assert request["ProcessingInputs"][0]["S3Input"]["S3Uri"].endswith("issue=20261025T230000Z/")
    assert request["ProcessingOutputConfig"]["Outputs"][0]["S3Output"]["S3Uri"].endswith("chronos2/run=te-chronos2-live-20261025t2300z/")
    assert request["AppSpecification"]["ContainerArguments"][-1] == "prospective_inference"
    assert {tag["Value"] for tag in request["Tags"] if tag["Key"] == "run-kind"} == {"prospective-inference"}
