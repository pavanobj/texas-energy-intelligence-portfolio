from __future__ import annotations

import copy
from pathlib import Path

import pytest

from texas_energy.mlops.model_lifecycle import (
    ModelLifecycleError,
    evaluate_challenger,
    rollback_selection,
    resolve_approved_artifact,
    validate_candidate_manifest,
)
from scripts.prepare_model_promotion import prepare_selection


def _candidate(*, candidate_id: str, model: str = "xgboost", mae: float = 100.0, rmse: float = 120.0, count: int = 48) -> dict[str, object]:
    files = {"weather_aware.json": "a" * 64, "model_contract.json": "b" * 64} if model == "xgboost" else {
        "weather_aware_best.pt": "a" * 64, "preprocessing.json": "b" * 64, "model_contract.json": "c" * 64,
    }
    return {
        "schema_version": "texas-energy-candidate-model-v1", "candidate_id": candidate_id,
        "model": model, "promotion_status": "PendingManualApproval",
        "training": {"job_arn": f"arn:example:sagemaker:us-east-1:123:training-job/{candidate_id}", "status": "Completed", "accelerator": "gpu" if model == "bilstm" else "cpu"},
        "dataset": {"manifest_uri": "s3://bucket/training/v1/datasets/dataset=abc/manifest/", "manifest_sha256": "d" * 64, "split_contract_sha256": "e" * 64},
        "artifact": {"training_output_uri": f"s3://bucket/models/candidates/{candidate_id}/", "model_tar_sha256": "f" * 64, "required_files_sha256": files},
        "evaluation": {"forecast_horizon_hours": 24, "feature_contract": "direct-weather-v1", "held_out_metrics": {"mae_mw": mae, "rmse_mw": rmse, "bias_mw": -5.0, "count": count}},
    }


def _selection() -> dict[str, object]:
    return {
        "schema_version": "texas-energy-approved-model-selection-v1", "status": "approved",
        "models": {
            "xgboost": {"model": "xgboost", "version_id": "legacy-xgb", "artifact_relative_path": "xgboost/version=legacy", "required_files_sha256": {"weather_aware.json": "a" * 64}},
            "bilstm": {"model": "bilstm", "version_id": "legacy-bilstm", "artifact_relative_path": "bilstm/version=legacy", "required_files_sha256": {"weather_aware_best.pt": "b" * 64}},
        },
    }


def test_candidate_requires_completed_gpu_bilstm_and_full_held_out_contract() -> None:
    candidate = _candidate(candidate_id="bilstm-1", model="bilstm")
    assert validate_candidate_manifest(candidate)["candidate_id"] == "bilstm-1"
    candidate["training"]["accelerator"] = "cpu"
    with pytest.raises(ModelLifecycleError, match="GPU"):
        validate_candidate_manifest(candidate)


def test_challenger_refuses_metrics_from_a_different_snapshot() -> None:
    champion = _candidate(candidate_id="champion")
    challenger = _candidate(candidate_id="challenger", mae=90, rmse=110)
    challenger["dataset"]["manifest_sha256"] = "c" * 64
    with pytest.raises(ModelLifecycleError, match="different datasets"):
        evaluate_challenger(champion=champion, challenger=challenger)


def test_eligible_challenger_still_needs_human_approval_and_resolves_safely(tmp_path: Path) -> None:
    champion = _candidate(candidate_id="champion")
    challenger = _candidate(candidate_id="challenger", mae=90, rmse=110)
    decision = evaluate_challenger(champion=champion, challenger=challenger)
    assert decision["decision"] == "eligible_for_manual_approval"
    updated = prepare_selection(
        current=_selection(), candidate=challenger, decision=decision, approved_by="pavan",
        approval_reason="matched held-out metrics improved", artifact_relative_path="xgboost/version=challenger",
    )
    root = tmp_path / "approved"; (root / "xgboost" / "version=challenger").mkdir(parents=True)
    artifact, entry = resolve_approved_artifact(selection=updated, model="xgboost", artifacts_root=root)
    assert artifact == (root / "xgboost" / "version=challenger").resolve()
    assert entry["version_id"] == "challenger"


def test_promotion_rejects_degraded_or_unapproved_evaluation() -> None:
    champion = _candidate(candidate_id="champion")
    challenger = _candidate(candidate_id="challenger", mae=101, rmse=121)
    rejected = evaluate_challenger(champion=champion, challenger=challenger)
    with pytest.raises(ModelLifecycleError, match="eligible challenger"):
        prepare_selection(
            current=_selection(), candidate=challenger, decision=rejected, approved_by="pavan",
            approval_reason="try anyway", artifact_relative_path="xgboost/version=challenger",
        )


def test_rollback_restores_an_explicit_prior_verified_champion() -> None:
    prior = _selection()
    current = copy.deepcopy(prior)
    current["models"]["xgboost"]["version_id"] = "new-xgb"
    restored = rollback_selection(current=current, prior_verified=prior, approved_by="pavan", rollback_reason="regression detected")
    assert restored["models"]["xgboost"]["version_id"] == "legacy-xgb"
    assert restored["rollback_of"]["xgboost"] == "new-xgb"
