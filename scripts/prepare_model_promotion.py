"""Validate an approval and render the next immutable model-selection pointer.

This command never calls AWS. A later reviewed GitHub Actions workflow may
upload the rendered selection after it has uploaded and verified the matching
artifact directory. It also supports a dry-run, which makes approval and
rollback checks safe on a developer machine or CI runner.
"""

from __future__ import annotations

import argparse
import hashlib
from datetime import datetime, timezone
from pathlib import Path

from texas_energy.mlops.model_lifecycle import (
    ModelLifecycleError,
    load_json,
    validate_approved_selection,
    validate_candidate_manifest,
    validate_evaluation_decision,
    write_new_json,
)


def _selection_entry(candidate: dict[str, object], *, artifact_relative_path: str) -> dict[str, object]:
    artifact = candidate["artifact"]
    assert isinstance(artifact, dict)
    return {
        "model": candidate["model"],
        "version_id": candidate["candidate_id"],
        "artifact_relative_path": artifact_relative_path,
        "required_files_sha256": artifact["required_files_sha256"],
        "training_job_arn": candidate["training"]["job_arn"],
        "dataset_manifest_sha256": candidate["dataset"]["manifest_sha256"],
        "feature_contract": candidate["evaluation"]["feature_contract"],
    }


def _verify_artifact_dir(candidate: dict[str, object], artifact_dir: Path) -> None:
    expected = candidate["artifact"]["required_files_sha256"]
    for name, digest in expected.items():
        path = artifact_dir / name
        if not path.is_file():
            raise ModelLifecycleError(f"Candidate artifact is missing {name}.")
        actual = hashlib.sha256(path.read_bytes()).hexdigest()
        if actual != digest:
            raise ModelLifecycleError(f"Candidate artifact checksum differs for {name}.")


def prepare_selection(*, current: dict[str, object], candidate: dict[str, object], decision: dict[str, object], approved_by: str, approval_reason: str, artifact_relative_path: str, artifact_dir: Path | None = None) -> dict[str, object]:
    current = validate_approved_selection(current)
    candidate = validate_candidate_manifest(candidate)
    if artifact_dir is not None:
        _verify_artifact_dir(candidate, artifact_dir)
    decision = validate_evaluation_decision(decision)
    if decision["decision"] != "eligible_for_manual_approval":
        raise ModelLifecycleError("Only an eligible challenger may be promoted.")
    if decision["challenger"]["candidate_id"] != candidate["candidate_id"]:
        raise ModelLifecycleError("Evaluation decision does not describe the requested candidate.")
    if not approved_by.strip() or not approval_reason.strip():
        raise ModelLifecycleError("Promotion requires a non-empty approver and reason.")
    model = str(candidate["model"])
    next_selection = dict(current)
    next_selection["models"] = dict(current["models"])
    next_selection["models"][model] = _selection_entry(candidate, artifact_relative_path=artifact_relative_path)
    next_selection["approved_at_utc"] = datetime.now(timezone.utc).isoformat()
    next_selection["approved_by"] = approved_by
    next_selection["approval_reason"] = approval_reason
    next_selection["previous_version_id"] = current["models"][model]["version_id"]
    return validate_approved_selection(next_selection)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--current-selection", type=Path, required=True)
    parser.add_argument("--candidate", type=Path, required=True)
    parser.add_argument("--evaluation", type=Path, required=True)
    parser.add_argument("--approved-by", required=True)
    parser.add_argument("--approval-reason", required=True)
    parser.add_argument("--artifact-relative-path", required=True)
    parser.add_argument("--artifact-dir", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    rendered = prepare_selection(
        current=load_json(args.current_selection), candidate=load_json(args.candidate),
        decision=load_json(args.evaluation), approved_by=args.approved_by,
        approval_reason=args.approval_reason, artifact_relative_path=args.artifact_relative_path, artifact_dir=args.artifact_dir,
    )
    if args.dry_run:
        print(f"DRY-RUN: would write {args.output} with {rendered['models'][rendered['models'].keys().__iter__().__next__()]['version_id']}")
        return 0
    write_new_json(args.output, rendered)
    print(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
