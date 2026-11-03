"""Immutable candidate, evaluation, and approved-model selection contracts.

This module intentionally has no AWS SDK dependency. It validates the JSON
documents that a reviewed GitHub Actions workflow later reads or writes. That
keeps candidate creation and promotion deterministic, auditable, and testable
without changing a live model selection.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


TRAINABLE_MODELS = ("xgboost", "bilstm")
_SHA256 = re.compile(r"^[a-f0-9]{64}$")
_ID = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


class ModelLifecycleError(ValueError):
    """A lifecycle document is incomplete, incompatible, or unsafe."""


def canonical_json_sha256(payload: dict[str, Any]) -> str:
    """Hash JSON deterministically, excluding no fields on the caller's behalf."""

    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ModelLifecycleError(f"Unreadable lifecycle document: {path}") from error
    if not isinstance(payload, dict):
        raise ModelLifecycleError("Lifecycle documents must be JSON objects.")
    return payload


def write_new_json(path: Path, payload: dict[str, Any]) -> None:
    """Create an immutable local document and refuse an accidental overwrite."""

    if path.exists():
        raise ModelLifecycleError(f"Refusing to overwrite immutable lifecycle document: {path}")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _require_sha(value: object, field: str) -> str:
    if not isinstance(value, str) or not _SHA256.fullmatch(value):
        raise ModelLifecycleError(f"{field} must be a lowercase SHA-256 digest.")
    return value


def _require_id(value: object, field: str) -> str:
    if not isinstance(value, str) or not _ID.fullmatch(value):
        raise ModelLifecycleError(f"{field} must be a safe non-empty identifier.")
    return value


def _require_s3_prefix(value: object, field: str) -> str:
    if not isinstance(value, str) or not value.startswith("s3://") or not value.endswith("/"):
        raise ModelLifecycleError(f"{field} must be an S3 prefix ending in '/'.")
    return value


def validate_candidate_manifest(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate one candidate emitted from a Completed managed training job."""

    if payload.get("schema_version") != "texas-energy-candidate-model-v1":
        raise ModelLifecycleError("Unsupported candidate manifest schema.")
    model = payload.get("model")
    if model not in TRAINABLE_MODELS:
        raise ModelLifecycleError("Candidate model must be xgboost or bilstm.")
    _require_id(payload.get("candidate_id"), "candidate_id")
    if payload.get("promotion_status") != "PendingManualApproval":
        raise ModelLifecycleError("New candidates must remain PendingManualApproval.")
    training = payload.get("training")
    dataset = payload.get("dataset")
    artifact = payload.get("artifact")
    evaluation = payload.get("evaluation")
    if not all(isinstance(value, dict) for value in (training, dataset, artifact, evaluation)):
        raise ModelLifecycleError("Candidate manifest lacks training, dataset, artifact, or evaluation evidence.")
    if training.get("status") != "Completed" or not isinstance(training.get("job_arn"), str):
        raise ModelLifecycleError("Candidate must cite a Completed SageMaker training job ARN.")
    if model == "bilstm" and training.get("accelerator") != "gpu":
        raise ModelLifecycleError("Bi-LSTM candidate provenance must prove GPU training.")
    _require_s3_prefix(dataset.get("manifest_uri"), "dataset.manifest_uri")
    _require_sha(dataset.get("manifest_sha256"), "dataset.manifest_sha256")
    _require_sha(dataset.get("split_contract_sha256"), "dataset.split_contract_sha256")
    _require_s3_prefix(artifact.get("training_output_uri"), "artifact.training_output_uri")
    _require_sha(artifact.get("model_tar_sha256"), "artifact.model_tar_sha256")
    if not isinstance(artifact.get("required_files_sha256"), dict) or not artifact["required_files_sha256"]:
        raise ModelLifecycleError("Candidate must record required artifact file checksums.")
    for name, digest in artifact["required_files_sha256"].items():
        if not isinstance(name, str) or "/" in name:
            raise ModelLifecycleError("Artifact checksum keys must be file names.")
        _require_sha(digest, f"artifact.required_files_sha256[{name!r}]")
    if evaluation.get("forecast_horizon_hours") != 24 or evaluation.get("feature_contract") is None:
        raise ModelLifecycleError("Candidate must prove the 24-hour feature contract.")
    metrics = evaluation.get("held_out_metrics")
    if not isinstance(metrics, dict) or not all(key in metrics for key in ("mae_mw", "rmse_mw", "bias_mw", "count")):
        raise ModelLifecycleError("Candidate requires held-out MAE, RMSE, bias, and coverage.")
    return deepcopy(payload)


def validate_evaluation_decision(payload: dict[str, Any]) -> dict[str, Any]:
    if payload.get("schema_version") != "texas-energy-champion-challenger-evaluation-v1":
        raise ModelLifecycleError("Unsupported evaluation decision schema.")
    if payload.get("decision") not in {"eligible_for_manual_approval", "ineligible", "rejected"}:
        raise ModelLifecycleError("Evaluation decision is invalid.")
    compatibility = payload.get("comparison_contract")
    if not isinstance(compatibility, dict) or compatibility.get("same_dataset_manifest") is not True or compatibility.get("same_split_contract") is not True:
        raise ModelLifecycleError("Champion and challenger must use the identical dataset and split contract.")
    for side in ("champion", "challenger"):
        if not isinstance(payload.get(side), dict):
            raise ModelLifecycleError(f"Evaluation lacks {side} evidence.")
        metrics = payload[side].get("held_out_metrics")
        if not isinstance(metrics, dict) or not all(name in metrics for name in ("mae_mw", "rmse_mw", "bias_mw", "count")):
            raise ModelLifecycleError(f"Evaluation lacks held-out metrics for {side}.")
    return deepcopy(payload)


def evaluate_challenger(*, champion: dict[str, Any], challenger: dict[str, Any]) -> dict[str, Any]:
    """Compare like-for-like candidates and return a non-promoting decision."""

    champion = validate_candidate_manifest(champion)
    challenger = validate_candidate_manifest(challenger)
    if champion["model"] != challenger["model"]:
        raise ModelLifecycleError("Champion and challenger model families must match.")
    same_dataset = champion["dataset"]["manifest_sha256"] == challenger["dataset"]["manifest_sha256"]
    same_split = champion["dataset"]["split_contract_sha256"] == challenger["dataset"]["split_contract_sha256"]
    if not same_dataset or not same_split:
        raise ModelLifecycleError("Refusing to compare candidates from different datasets or chronological splits.")
    champion_metrics = champion["evaluation"]["held_out_metrics"]
    challenger_metrics = challenger["evaluation"]["held_out_metrics"]
    same_coverage = champion_metrics["count"] == challenger_metrics["count"]
    degraded = (
        not same_coverage
        or float(challenger_metrics["mae_mw"]) > float(champion_metrics["mae_mw"])
        or float(challenger_metrics["rmse_mw"]) > float(champion_metrics["rmse_mw"])
    )
    return {
        "schema_version": "texas-energy-champion-challenger-evaluation-v1",
        "model": challenger["model"],
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "comparison_contract": {
            "dataset_manifest_sha256": challenger["dataset"]["manifest_sha256"],
            "split_contract_sha256": challenger["dataset"]["split_contract_sha256"],
            "same_dataset_manifest": same_dataset,
            "same_split_contract": same_split,
            "same_held_out_coverage": same_coverage,
        },
        "champion": {"candidate_id": champion["candidate_id"], "held_out_metrics": champion_metrics},
        "challenger": {"candidate_id": challenger["candidate_id"], "held_out_metrics": challenger_metrics},
        "decision": "rejected" if degraded else "eligible_for_manual_approval",
        "reason": "Challenger degrades matched held-out MAE/RMSE or coverage." if degraded else "Comparable challenger improves or matches matched held-out MAE/RMSE. Human approval is still required.",
        "automatic_promotion": False,
    }


def validate_approved_selection(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate the single future production model pointer."""

    if payload.get("schema_version") != "texas-energy-approved-model-selection-v1":
        raise ModelLifecycleError("Unsupported approved-model selection schema.")
    if payload.get("status") != "approved":
        raise ModelLifecycleError("Only an explicitly approved selection is loadable.")
    entries = payload.get("models")
    if not isinstance(entries, dict) or set(entries) != set(TRAINABLE_MODELS):
        raise ModelLifecycleError("Approved selection must contain exactly XGBoost and Bi-LSTM.")
    for model, item in entries.items():
        if not isinstance(item, dict) or item.get("model") != model:
            raise ModelLifecycleError(f"Selection entry does not match {model}.")
        _require_id(item.get("version_id"), f"models.{model}.version_id")
        relative = item.get("artifact_relative_path")
        if not isinstance(relative, str) or not relative or relative.startswith("/") or ".." in Path(relative).parts:
            raise ModelLifecycleError(f"models.{model}.artifact_relative_path is unsafe.")
        if not isinstance(item.get("required_files_sha256"), dict):
            raise ModelLifecycleError(f"Selection entry for {model} lacks artifact integrity evidence.")
        for digest in item["required_files_sha256"].values():
            _require_sha(digest, f"models.{model}.required_files_sha256")
    return deepcopy(payload)


def resolve_approved_artifact(*, selection: dict[str, Any], model: str, artifacts_root: Path) -> tuple[Path, dict[str, Any]]:
    """Resolve one selected artifact without accepting a latest-candidate fallback."""

    checked = validate_approved_selection(selection)
    if model not in TRAINABLE_MODELS:
        raise ModelLifecycleError("Only trainable model families use the approved-model selector.")
    entry = checked["models"][model]
    artifact = (artifacts_root / entry["artifact_relative_path"]).resolve()
    root = artifacts_root.resolve()
    if root not in artifact.parents and artifact != root:
        raise ModelLifecycleError("Resolved artifact escapes the approved artifact root.")
    return artifact, entry


def rollback_selection(*, current: dict[str, Any], prior_verified: dict[str, Any], approved_by: str, rollback_reason: str) -> dict[str, Any]:
    """Return a prior verified selection with a new, explicit rollback audit."""

    validate_approved_selection(current)
    restored = validate_approved_selection(prior_verified)
    if not approved_by.strip() or not rollback_reason.strip():
        raise ModelLifecycleError("Rollback requires a non-empty approver and reason.")
    result = deepcopy(restored)
    result["approved_at_utc"] = datetime.now(timezone.utc).isoformat()
    result["approved_by"] = approved_by
    result["approval_reason"] = rollback_reason
    result["rollback_of"] = {model: current["models"][model]["version_id"] for model in TRAINABLE_MODELS}
    return validate_approved_selection(result)
