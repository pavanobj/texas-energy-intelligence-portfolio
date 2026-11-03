"""Build, but never submit, a bounded managed weather-aware training request."""

from __future__ import annotations

import argparse
import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any


_MODELS = ("xgboost", "bilstm")
_JOB_NAME = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_GIT_REVISION = re.compile(r"^[a-f0-9]{40}$")


class TrainingRequestError(ValueError):
    """A managed-training request does not satisfy the immutable channel contract."""


def _prefix(uri: str, *, field: str) -> str:
    if not uri.startswith("s3://") or not uri.endswith("/"):
        raise TrainingRequestError(f"{field} must be a versioned S3 prefix ending with '/'.")
    return uri


def _utc_timestamp(value: str, *, field: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise TrainingRequestError(f"{field} must be an ISO-8601 UTC timestamp.") from error
    if parsed.tzinfo is None:
        raise TrainingRequestError(f"{field} must include a UTC offset.")
    return parsed


def build_request(
    config_path: Path, *, model: str, job_name: str, image_uri: str, role_arn: str,
    gold_uri: str, weather_uri: str, output_uri: str, train_target_end: str,
    validation_target_end: str, dataset_manifest_uri: str, dataset_manifest_sha256: str,
    git_revision: str, seed: int = 42, epochs: int = 12, context_hours: int = 168,
) -> dict[str, Any]:
    if model not in _MODELS:
        raise TrainingRequestError("Only XGBoost and Bi-LSTM are trainable; Chronos-2 is zero-shot.")
    if not _JOB_NAME.fullmatch(job_name):
        raise TrainingRequestError("Training job name must be 1-63 alphanumeric/hyphen characters.")
    if "@sha256:" not in image_uri:
        raise TrainingRequestError("Training image must be an immutable ECR digest URI.")
    if epochs <= 0 or seed < 0 or context_hours < 24:
        raise TrainingRequestError("epochs must be positive, seed non-negative, and context_hours at least 24.")
    if not _GIT_REVISION.fullmatch(git_revision):
        raise TrainingRequestError("git_revision must be the full immutable 40-character commit SHA.")
    if not _SHA256.fullmatch(dataset_manifest_sha256):
        raise TrainingRequestError("dataset_manifest_sha256 must be a lowercase SHA-256 digest.")
    if _utc_timestamp(train_target_end, field="train_target_end") >= _utc_timestamp(
        validation_target_end, field="validation_target_end"
    ):
        raise TrainingRequestError("train_target_end must be earlier than validation_target_end.")
    payload = json.loads(config_path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "sagemaker-candidate-training-v2":
        raise TrainingRequestError("Unsupported training configuration schema.")
    jobs = {item["model"]: item for item in payload["jobs"]}
    job = jobs[model]
    if model == "bilstm" and (
        job.get("accelerator") != "gpu" or not str(job.get("instance_type", "")).startswith("ml.g")
    ):
        raise TrainingRequestError("Bi-LSTM candidate training requires an explicitly configured GPU instance.")
    execution = payload["execution"]
    if image_uri != execution.get("image_uri"):
        raise TrainingRequestError("Training image must equal the reviewed immutable candidate runtime digest in configuration.")
    dataset_prefix = _prefix(payload["candidate_inputs"]["prefix"], field="candidate_inputs.prefix")
    for field, uri in (("gold_uri", gold_uri), ("weather_uri", weather_uri), ("dataset_manifest_uri", dataset_manifest_uri)):
        if not _prefix(uri, field=field).startswith(dataset_prefix):
            raise TrainingRequestError(f"{field} must be under the isolated candidate dataset prefix.")
    output_uri = _prefix(output_uri, field="output_uri")
    expected_output = f"{_prefix(execution['output_prefix'], field='execution.output_prefix')}{model}/run={job_name}/"
    if output_uri != expected_output:
        raise TrainingRequestError("output_uri must be the deterministic isolated candidate output prefix for job_name.")
    channels = []
    for name, uri in (("gold", gold_uri), ("weather", weather_uri), ("manifest", dataset_manifest_uri)):
        channels.append({
            "ChannelName": name,
            "InputMode": "File",
            "DataSource": {"S3DataSource": {
                "S3DataType": "S3Prefix", "S3Uri": _prefix(uri, field=f"{name}_uri"),
                "S3DataDistributionType": "FullyReplicated",
            }},
        })
    hyperparameters = {
        "model": model, "split-mode": "chronological", "train-target-end": train_target_end,
        "validation-target-end": validation_target_end, "seed": str(seed),
        "context-hours": str(context_hours), "git-revision": git_revision,
        "dataset-manifest-uri": dataset_manifest_uri,
        "dataset-manifest-sha256": dataset_manifest_sha256,
    }
    if model == "bilstm":
        hyperparameters["epochs"] = str(epochs)
    return {
        "TrainingJobName": job_name,
        "RoleArn": role_arn,
        "AlgorithmSpecification": {
            "TrainingImage": image_uri,
            "TrainingInputMode": "File",
            "ContainerEntrypoint": ["python", "-m", "texas_energy.sagemaker.training_runtime"],
            "ContainerArguments": [
                "--model", model,
                "--split-mode", "chronological",
                "--train-target-end", train_target_end,
                "--validation-target-end", validation_target_end,
                "--seed", str(seed),
                "--context-hours", str(context_hours),
                "--git-revision", git_revision,
                "--dataset-manifest-uri", dataset_manifest_uri,
                "--dataset-manifest-sha256", dataset_manifest_sha256,
                "--dataset-manifest-path", "/opt/ml/input/data/manifest/dataset_manifest.json",
                "--experiment-name", payload["experiment"]["name"],
                "--training-job-name", job_name,
            ] + (["--epochs", str(epochs)] if model == "bilstm" else []),
        },
        "InputDataConfig": channels,
        "OutputDataConfig": {"S3OutputPath": output_uri},
        "ResourceConfig": {"InstanceCount": 1, "InstanceType": job["instance_type"], "VolumeSizeInGB": 50},
        "StoppingCondition": {"MaxRuntimeInSeconds": int(execution["max_runtime_seconds"])},
        "HyperParameters": hyperparameters,
        "ExperimentConfig": {
            "ExperimentName": payload["experiment"]["name"],
            "TrialName": job_name,
            "TrialComponentDisplayName": job_name,
        },
        "EnableNetworkIsolation": True,
        "Tags": [
            {"Key": "project", "Value": "texas-energy-intelligence"},
            {"Key": "model", "Value": model},
            {"Key": "run-kind", "Value": "candidate-training"},
            {"Key": "promotion-status", "Value": "candidate-only"},
            {"Key": "git-revision", "Value": git_revision},
            {"Key": "dataset-manifest-sha256", "Value": dataset_manifest_sha256},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/sagemaker/training-jobs.json"))
    parser.add_argument("--model", choices=_MODELS, required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--image-uri", required=True)
    parser.add_argument("--role-arn", required=True)
    parser.add_argument("--gold-uri", required=True)
    parser.add_argument("--weather-uri", required=True)
    parser.add_argument("--output-uri", required=True)
    parser.add_argument("--train-target-end", required=True)
    parser.add_argument("--validation-target-end", required=True)
    parser.add_argument("--dataset-manifest-uri", required=True)
    parser.add_argument("--dataset-manifest-sha256", required=True)
    parser.add_argument("--git-revision", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--epochs", type=int, default=12)
    parser.add_argument("--context-hours", type=int, default=168)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = build_request(
        args.config, model=args.model, job_name=args.job_name, image_uri=args.image_uri,
        role_arn=args.role_arn, gold_uri=args.gold_uri, weather_uri=args.weather_uri,
        output_uri=args.output_uri, train_target_end=args.train_target_end,
        validation_target_end=args.validation_target_end, dataset_manifest_uri=args.dataset_manifest_uri,
        dataset_manifest_sha256=args.dataset_manifest_sha256, git_revision=args.git_revision,
        seed=args.seed, epochs=args.epochs, context_hours=args.context_hours,
    )
    args.output.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
