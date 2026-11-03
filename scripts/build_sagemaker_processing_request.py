"""Build a deterministic SageMaker Processing request for one validated model.

The request is deliberately emitted as JSON for review before `CreateProcessingJob`.
It contains no AWS credentials and cannot launch work by itself.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


MODELS = ("xgboost", "bilstm", "chronos2")
_JOB_NAME = re.compile(r"^[a-zA-Z0-9](-*[a-zA-Z0-9]){0,62}$")


class ProcessingRequestError(ValueError):
    """The reviewed configuration cannot form a safe bounded job request."""


def _load(path: Path) -> dict[str, Any]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if payload.get("schema_version") != "sagemaker-unified-jobs-v2":
        raise ProcessingRequestError("Unsupported SageMaker job configuration schema.")
    return payload


def _s3_input(name: str, uri: str, local_path: str) -> dict[str, Any]:
    if not uri.startswith("s3://") or not uri.endswith("/"):
        raise ProcessingRequestError(f"{name} must use a versioned S3 prefix URI.")
    return {
        "InputName": name,
        "AppManaged": False,
        "S3Input": {
            "S3Uri": uri,
            "LocalPath": local_path,
            "S3DataType": "S3Prefix",
            "S3InputMode": "File",
            "S3DataDistributionType": "FullyReplicated",
            "S3CompressionType": "None",
        },
    }


def build_request(
    config_path: Path,
    *,
    model: str,
    job_name: str,
    loader_revision: str,
    input_uri: str | None = None,
    output_prefix: str | None = None,
    generation_type: str = "historical_replay",
) -> dict[str, Any]:
    if model not in MODELS:
        raise ProcessingRequestError(f"Unsupported model: {model}")
    if not _JOB_NAME.fullmatch(job_name):
        raise ProcessingRequestError("Processing job name must be 1-63 alphanumeric/hyphen characters.")
    config = _load(config_path)
    jobs = {item["model"]: item for item in config["jobs"]}
    job = jobs.get(model)
    if job is None:
        raise ProcessingRequestError(f"No configuration exists for {model}.")
    execution = config["managed_execution"]
    if execution.get("job_type") != "sagemaker_processing" or "@sha256:" not in execution["image_uri"]:
        raise ProcessingRequestError("Managed execution must be pinned to an immutable Processing image digest.")
    if generation_type not in {"historical_replay", "prospective_inference"}:
        raise ProcessingRequestError("generation type must be historical_replay or prospective_inference.")
    assets = config["validation_assets"]
    resolved_input_uri = input_uri or assets["input_bundle"]
    resolved_output_prefix = output_prefix or execution["output_prefix"]
    artifact_uri = assets["model_versions"][model]
    if not resolved_output_prefix.startswith("s3://"):
        raise ProcessingRequestError("output prefix must use a versioned S3 prefix URI.")
    output_uri = f"{resolved_output_prefix.rstrip('/')}/{model}/run={job_name}/"
    return {
        "ProcessingJobName": job_name,
        "RoleArn": execution["execution_role_arn"],
        "AppSpecification": {
            "ImageUri": execution["image_uri"],
            # Dockerfile.sagemaker already owns the immutable runtime ENTRYPOINT.
            # Supplying it again through SageMaker turns it into duplicate CLI
            # arguments ("python -m ...") and prevents the dispatcher from
            # parsing --model.  Processing therefore supplies only arguments.
            "ContainerArguments": [
                "--model", model,
                "--input-bundle", "/opt/ml/processing/input",
                "--artifact-dir", "/opt/ml/processing/model",
                "--output-dir", "/opt/ml/processing/output",
                "--run-id", job_name,
                "--generation-type", generation_type,
            ],
        },
        "ProcessingInputs": [
            _s3_input("input", resolved_input_uri, "/opt/ml/processing/input"),
            _s3_input("model", artifact_uri, "/opt/ml/processing/model"),
        ],
        "ProcessingOutputConfig": {
            "Outputs": [{
                "OutputName": "forecast",
                "AppManaged": False,
                "S3Output": {"S3Uri": output_uri, "LocalPath": "/opt/ml/processing/output", "S3UploadMode": "EndOfJob"},
            }]
        },
        "ProcessingResources": {"ClusterConfig": {
            "InstanceCount": 1,
            "InstanceType": job["instance_type"],
            "VolumeSizeInGB": int(job["volume_size_gb"]),
        }},
        "StoppingCondition": {"MaxRuntimeInSeconds": int(job["max_runtime_seconds"])},
        "Environment": {"TEXAS_ENERGY_LOADER_GIT_REVISION": loader_revision},
        "Tags": [
            {"Key": "project", "Value": "texas-energy-intelligence"},
            {"Key": "model", "Value": model},
            {"Key": "run-kind", "Value": generation_type.replace("_", "-")},
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("config/sagemaker/jobs.json"))
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--job-name", required=True)
    parser.add_argument("--loader-git-revision", required=True)
    parser.add_argument("--input-uri", help="immutable live or replay input-bundle S3 prefix")
    parser.add_argument("--output-prefix", help="immutable model-output S3 prefix")
    parser.add_argument("--generation-type", choices=("historical_replay", "prospective_inference"), default="historical_replay")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    request = build_request(
        args.config, model=args.model, job_name=args.job_name, loader_revision=args.loader_git_revision,
        input_uri=args.input_uri, output_prefix=args.output_prefix, generation_type=args.generation_type,
    )
    args.output.write_text(json.dumps(request, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
