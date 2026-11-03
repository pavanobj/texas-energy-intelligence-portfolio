import importlib.util
from pathlib import Path

import pytest


_SPEC = importlib.util.spec_from_file_location(
    "build_sagemaker_training_request",
    Path(__file__).parents[1] / "scripts" / "build_sagemaker_training_request.py",
)
assert _SPEC and _SPEC.loader
_MODULE = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_MODULE)
build_request = _MODULE.build_request
TrainingRequestError = _MODULE.TrainingRequestError


def test_builds_immutable_weather_aware_training_request() -> None:
    request = build_request(
        Path("config/sagemaker/training-jobs.json"), model="bilstm", job_name="te-bilstm-train-1",
        image_uri="<AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/texas-energy-sagemaker-unified@sha256:6954e5105395b8acf54e6e81112d5a4261cba31390fb907a1352eb14b6b15854",
        role_arn="arn:example:iam::<AWS_ACCOUNT_ID>:role/texas-energy-sagemaker-unified-execution",
        gold_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/gold/", weather_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/weather/",
        output_uri="s3://texas-energy-data-lake-example/models/v2/candidates/bilstm/run=te-bilstm-train-1/",
        train_target_end="2024-12-31T23:00:00Z", validation_target_end="2025-06-30T23:00:00Z",
        dataset_manifest_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/manifest/",
        dataset_manifest_sha256="b" * 64, git_revision="a" * 40, epochs=8,
    )
    assert request["AlgorithmSpecification"]["ContainerEntrypoint"][-1] == "texas_energy.sagemaker.training_runtime"
    assert request["HyperParameters"]["split-mode"] == "chronological"
    assert request["HyperParameters"]["epochs"] == "8"
    assert request["InputDataConfig"][1]["ChannelName"] == "weather"
    assert request["InputDataConfig"][2]["ChannelName"] == "manifest"
    assert request["InputDataConfig"][0]["InputMode"] == "File"
    assert "S3InputMode" not in request["InputDataConfig"][0]["DataSource"]["S3DataSource"]
    assert "MetricDefinitions" not in request
    assert request["AlgorithmSpecification"]["ContainerArguments"][:2] == ["--model", "bilstm"]
    assert request["ExperimentConfig"]["ExperimentName"] == "texas-energy-candidate-training"
    assert "--training-job-name" in request["AlgorithmSpecification"]["ContainerArguments"]
    assert request["EnableNetworkIsolation"] is True
    assert request["ResourceConfig"]["InstanceType"] == "ml.g4dn.xlarge"


def test_rejects_chronos_or_mutable_image() -> None:
    common = dict(
        config_path=Path("config/sagemaker/training-jobs.json"), job_name="te-train-1", image_uri="tag-only",
        role_arn="role", gold_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/gold/", weather_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/weather/",
        output_uri="s3://texas-energy-data-lake-example/models/v2/candidates/xgboost/run=te-train-1/", train_target_end="2024-01-01T00:00:00Z", validation_target_end="2025-01-01T00:00:00Z",
        dataset_manifest_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/manifest/", dataset_manifest_sha256="b" * 64, git_revision="a" * 40,
    )
    with pytest.raises(TrainingRequestError, match="Only XGBoost"):
        build_request(model="chronos2", **common)
    with pytest.raises(TrainingRequestError, match="immutable"):
        build_request(model="xgboost", **common)


def test_rejects_non_candidate_output_or_nonchronological_boundaries() -> None:
    common = dict(
        config_path=Path("config/sagemaker/training-jobs.json"), model="xgboost", job_name="te-train-1",
        image_uri="<AWS_ACCOUNT_ID>.dkr.ecr.us-east-1.amazonaws.com/texas-energy-sagemaker-unified@sha256:6954e5105395b8acf54e6e81112d5a4261cba31390fb907a1352eb14b6b15854",
        role_arn="role", gold_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/gold/",
        weather_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/weather/",
        output_uri="s3://texas-energy-data-lake-example/models/v2/candidates/xgboost/run=te-train-1/",
        dataset_manifest_uri="s3://texas-energy-data-lake-example/training/v1/datasets/run=one/manifest/",
        dataset_manifest_sha256="b" * 64, git_revision="a" * 40,
    )
    with pytest.raises(TrainingRequestError, match="earlier"):
        build_request(train_target_end="2025-01-01T00:00:00Z", validation_target_end="2024-01-01T00:00:00Z", **common)
    with pytest.raises(TrainingRequestError, match="deterministic"):
        build_request(
            train_target_end="2024-01-01T00:00:00Z", validation_target_end="2025-01-01T00:00:00Z",
            output_uri="s3://texas-energy-data-lake-example/models/v1/xgboost/production/", **{
                key: value for key, value in common.items() if key != "output_uri"
            },
        )
