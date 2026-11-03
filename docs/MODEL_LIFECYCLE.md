# Model lifecycle

## Current production state

The scheduled Step Functions workflow still uses its frozen `models/v1` XGBoost and Bi-LSTM prefixes. It is intentionally unchanged by this work. Chronos-2 remains a separately versioned pretrained zero-shot artifact and is not a candidate-training or promotion target.

Managed training writes immutable candidate artifacts under `models/v2/candidates/`, SageMaker Experiment lineage, training summaries, model contracts, metrics, preprocessing, predictions, and checksums. A completed training job is a candidate only, never a production release.

## Candidate dataset contract

`scripts/build_candidate_training_dataset.py --issued-weather-root ...` creates the next full-history dataset beneath `training/v1/datasets/dataset=<hash>/`. It refuses noncausal or incomplete five-city weather origins and demand gaps. Its v2 manifest records the Gold observation cutoff, eligible origin count, source checksums, exact schema files, and whole-curve chronological split boundaries. The 52,607 current Gold hours and 652 eligible issued-weather origins remain distinct coverage figures.

The existing `b2076d346e5093119fa0667c` dataset is preserved as immutable historical evidence. It is not retroactively rewritten. A future refresh must create a new manifest and use that manifest hash in every training request.

## Candidate and evaluation flow

1. Build a SageMaker request with `scripts/build_sagemaker_training_request.py`. XGBoost is CPU; Bi-LSTM requires a GPU and CUDA.
2. After a `Completed` job, download its immutable output only for checksum verification and run `scripts/build_candidate_model_manifest.py`.
3. Compare a candidate only with a champion that has the identical dataset manifest hash, whole-curve split hash, and held-out coverage using `scripts/evaluate_model_challenger.py`.
4. The evaluation document can be `eligible_for_manual_approval` but cannot promote anything itself.
5. `scripts/prepare_model_promotion.py --dry-run` verifies an explicit approver, reason, compatible evaluation, and a replacement selection document without contacting AWS.

Different smoke metrics are not comparable champion-versus-challenger evidence. The October 30 one-epoch candidates therefore remain `PendingManualApproval` and cannot be promoted from their existing metrics alone.

## Future approved selection

The approved-model selection is one JSON document, not SageMaker Model Registry plus a second competing source of truth. Registry would not improve the existing batch Processing loader, immutable S3 model artifacts, or SageMaker Experiments lineage at this stage. The future selection document records only approved XGBoost and Bi-LSTM version directories, checksums, training job, dataset hash, and feature contract.

The unified inference runtime already supports an optional `--approved-model-selection` channel. It resolves the selected version inside an approved-artifact root and rejects missing or mismatched checksums. It never falls back to the latest candidate. Existing jobs do not pass that option and retain their legacy static model inputs.

To activate the selection mechanism later, a reviewed deployment must first seed verified copies of the two current champions under `models/v1/approved/artifacts/`, upload the initial selection JSON, then deploy the matching runtime and Step Functions input revision together. Only then may a GitHub Actions promotion workflow upload a fully verified new artifact directory first and update the selection pointer last. Existing jobs retain their start-time download; a failed upload leaves the prior pointer untouched. Rollback selects the prior verified version with the same approval trail.

## Required approval before live change

No production model selection, processing input, IAM policy, or scheduled workflow has changed. A live integration request needs explicit authorization for the one-time approved-artifact seeding, narrowly scoped S3 write IAM, controlled Terraform deployment, and a bounded inference verification. No new training job is required for that integration step.
