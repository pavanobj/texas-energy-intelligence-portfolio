"""Managed-training dispatcher for the genuine weather-aware curve trainers.

This module deliberately delegates to the reviewed local training scripts.  It
does not replace their issued-weather joins, chronological split rules,
train-only scaling, or checkpoint/artifact formats with a simplified
SageMaker-specific implementation.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Sequence


TRAINABLE_MODELS = ("xgboost", "bilstm")
_CANDIDATE_DATASET_PREFIX = "s3://texas-energy-data-lake-example/training/v1/datasets/"
_HEX = re.compile(r"^[a-f0-9]+$")


class TrainingRuntimeError(RuntimeError):
    """The managed training channel contract cannot safely invoke a trainer."""


def _program_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _publish_output_data(*, output_data_dir: Path, lineage: dict[str, object], metrics: dict[str, object], target: Path) -> None:
    """Write compact, durable candidate evidence to SageMaker output data.

    SageMaker uploads this directory separately from ``/opt/ml/model``.  The
    summary deliberately records only facts known inside the container; the
    final TrainingJob ARN, billed duration, and terminal service status are
    added later from the control plane rather than invented here.
    """
    output_data_dir.mkdir(parents=True, exist_ok=True)
    summary = {
        "schema_version": "sagemaker-candidate-training-summary-v1",
        "status": "completed_in_container_pending_control_plane_finalization",
        "training_job_name": lineage["training_job_name"],
        "training_job_arn": None,
        "model": lineage["model"],
        "experiment_name": lineage["experiment_name"],
        "dataset_manifest_uri": lineage["dataset_manifest_uri"],
        "dataset_manifest_sha256": lineage["dataset_manifest_sha256"],
        "git_revision": lineage["git_revision"],
        "training_mode": lineage["training_mode"],
        "split_mode": lineage["split_mode"],
        "seed": lineage["seed"],
        "context_hours": lineage["context_hours"],
        "metrics": metrics["metrics"],
        "model_files_sha256": lineage["files_sha256"],
        "control_plane_metadata_pending": ["TrainingJobArn", "TrainingJobStatus", "TrainingTimeInSeconds", "BillableTimeInSeconds"],
    }
    (output_data_dir / "training_summary.json").write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    for name in ("metrics.json", "model_contract.json", "training_lineage.json"):
        shutil.copy2(target.parent / name if name == "training_lineage.json" else target / name, output_data_dir / name)


def _weather_aware_test_metrics(metrics: dict[str, object]) -> dict[str, object]:
    """Return test metrics from either reviewed trainer's persisted schema."""
    weather_aware = metrics["metrics"]["weather_aware"]
    return weather_aware.get("metrics", weather_aware)["test"]["model"]


def trainer_script(model: str) -> Path:
    if model not in TRAINABLE_MODELS:
        raise TrainingRuntimeError("Chronos-2 is pretrained zero-shot inference and is not a training job.")
    name = {
        "xgboost": "train_weather_aware_xgboost_curve.py",
        "bilstm": "train_weather_aware_bilstm_curve.py",
    }[model]
    # Installed CI packages do not retain repository-relative scripts, whereas
    # the immutable container has /opt/program as its working directory.  Use
    # the explicit image root when supplied, then the process working tree,
    # then the source checkout layout.
    roots = [
        Path(os.environ["TEXAS_ENERGY_PROGRAM_ROOT"]) if os.environ.get("TEXAS_ENERGY_PROGRAM_ROOT") else None,
        Path.cwd(),
        _program_root(),
    ]
    for root in (item for item in roots if item is not None):
        script = root / "scripts" / name
        if script.is_file():
            return script
    raise TrainingRuntimeError(f"Reviewed {model} trainer is absent from this immutable image.")


def build_command(
    *, model: str, gold_path: Path, weather_roots: Sequence[Path], output_dir: Path,
    split_mode: str, train_target_end: str, validation_target_end: str, seed: int,
    context_hours: int, epochs: int,
) -> list[str]:
    if not gold_path.is_file():
        raise TrainingRuntimeError("Gold channel must contain the requested immutable Gold Parquet snapshot.")
    if not weather_roots or any(not root.is_dir() for root in weather_roots):
        raise TrainingRuntimeError("Weather channel must provide at least one immutable issued-weather archive root.")
    if split_mode != "chronological":
        raise TrainingRuntimeError("Managed training permits only chronological whole-curve splits.")
    command = [
        sys.executable, str(trainer_script(model)), "--gold-path", str(gold_path),
        "--weather-root", *(str(root) for root in weather_roots), "--output-dir", str(output_dir),
        "--split-mode", split_mode, "--train-target-end", train_target_end,
        "--validation-target-end", validation_target_end,
        "--seed", str(seed),
    ]
    if model == "bilstm":
        if epochs <= 0 or context_hours < 24:
            raise TrainingRuntimeError("Bi-LSTM epochs must be positive and context_hours at least 24.")
        # Candidate Bi-LSTM training is GPU-only. The reviewed trainer fails
        # explicitly rather than silently falling back to CPU.
        command.extend(["--epochs", str(epochs), "--context-hours", str(context_hours), "--device", "cuda"])
    return command


def execute(args: argparse.Namespace) -> dict[str, object]:
    if not _HEX.fullmatch(args.git_revision) or len(args.git_revision) != 40:
        raise TrainingRuntimeError("git_revision must be the immutable 40-character commit SHA.")
    if not _HEX.fullmatch(args.dataset_manifest_sha256) or len(args.dataset_manifest_sha256) != 64:
        raise TrainingRuntimeError("dataset_manifest_sha256 must be a lowercase SHA-256 digest.")
    if not args.dataset_manifest_uri.startswith(_CANDIDATE_DATASET_PREFIX):
        raise TrainingRuntimeError("dataset_manifest_uri must remain in the isolated candidate dataset prefix.")
    if not args.dataset_manifest_path.is_file():
        raise TrainingRuntimeError("The immutable dataset manifest channel is missing dataset_manifest.json.")
    if _sha256(args.dataset_manifest_path) != args.dataset_manifest_sha256:
        raise TrainingRuntimeError("The downloaded dataset manifest does not match dataset_manifest_sha256.")
    model_dir = args.model_dir.resolve()
    model_dir.mkdir(parents=True, exist_ok=True)
    # SageMaker creates SM_MODEL_DIR before launch.  Train into a sibling
    # fresh directory and copy only completed artifacts into model.tar.gz.
    with tempfile.TemporaryDirectory(prefix=f"texas-energy-{args.model}-", dir=model_dir.parent) as temporary:
        candidate = Path(temporary) / "candidate"
        command = build_command(
            model=args.model, gold_path=args.gold_path, weather_roots=args.weather_root,
            output_dir=candidate, split_mode=args.split_mode,
            train_target_end=args.train_target_end,
            validation_target_end=args.validation_target_end, seed=args.seed,
            context_hours=args.context_hours, epochs=args.epochs,
        )
        subprocess.run(command, check=True)
        required = {
            "xgboost": ("weather_aware.json", "model_contract.json", "metrics.json", "predictions.parquet"),
            "bilstm": ("weather_aware_best.pt", "preprocessing.json", "model_contract.json", "metrics.json", "predictions.parquet"),
        }[args.model]
        missing = [name for name in required if not (candidate / name).is_file()]
        if missing:
            raise TrainingRuntimeError(f"Reviewed trainer did not produce required artifacts: {missing}")
        target = model_dir / "candidate"
        if target.exists():
            raise TrainingRuntimeError("SM_MODEL_DIR already contains a candidate; refusing overwrite.")
        shutil.copytree(candidate, target)
    files = {path.name: _sha256(path) for path in sorted(target.iterdir()) if path.is_file()}
    lineage = {
        "schema_version": "sagemaker-candidate-training-v2",
        "model": args.model,
        "training_job_name": args.training_job_name,
        "promotion_status": "candidate_only",
        "experiment_name": args.experiment_name,
        "git_revision": args.git_revision,
        "dataset_manifest_uri": args.dataset_manifest_uri,
        "dataset_manifest_sha256": args.dataset_manifest_sha256,
        "dataset_manifest_file": args.dataset_manifest_path.name,
        "seed": args.seed,
        "context_hours": args.context_hours,
        "training_mode": "weather_aware_direct_24h_curve",
        "horizon_hours": 24,
        "demand_availability_lag_hours": 24,
        "split_mode": args.split_mode,
        "whole_curve_split_policy": "A 24-hour curve crossing a split boundary is excluded.",
        "gold_snapshot_sha256": _sha256(args.gold_path),
        "trainer_script": trainer_script(args.model).name,
        "trainer_script_sha256": _sha256(trainer_script(args.model)),
        "files_sha256": files,
    }
    (model_dir / "training_lineage.json").write_text(json.dumps(lineage, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    metrics = json.loads((target / "metrics.json").read_text(encoding="utf-8"))
    _publish_output_data(output_data_dir=args.output_data_dir, lineage=lineage, metrics=metrics, target=target)
    for metric_name, metric_value in _weather_aware_test_metrics(metrics).items():
        if isinstance(metric_value, (int, float)) and metric_name in {"mae_mw", "rmse_mw", "bias_mw"}:
            print(f"SAGEMAKER_METRIC weather_aware_test_{metric_name}={metric_value}")
    return lineage


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=TRAINABLE_MODELS, required=True)
    parser.add_argument("--gold-path", type=Path, default=Path(os.environ.get("SM_CHANNEL_GOLD", "/opt/ml/input/data/gold")) / "gold.parquet")
    parser.add_argument("--weather-root", type=Path, nargs="+", default=[Path(os.environ.get("SM_CHANNEL_WEATHER", "/opt/ml/input/data/weather"))])
    parser.add_argument("--model-dir", type=Path, default=Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model")))
    parser.add_argument("--output-data-dir", type=Path, default=Path(os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data")))
    parser.add_argument("--split-mode", choices=("chronological",), default="chronological")
    parser.add_argument("--train-target-end", required=True)
    parser.add_argument("--validation-target-end", required=True)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--context-hours", type=int, default=168)
    parser.add_argument("--git-revision", required=True)
    parser.add_argument("--dataset-manifest-uri", required=True)
    parser.add_argument("--dataset-manifest-sha256", required=True)
    parser.add_argument("--dataset-manifest-path", type=Path, default=Path(os.environ.get("SM_CHANNEL_MANIFEST", "/opt/ml/input/data/manifest")) / "dataset_manifest.json")
    parser.add_argument("--experiment-name", required=True)
    parser.add_argument("--training-job-name", required=True)
    parser.add_argument("--epochs", type=int, default=12)
    return parser.parse_args()


def main() -> int:
    print(json.dumps(execute(arguments()), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
