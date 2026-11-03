from pathlib import Path

import pytest

import json

from texas_energy.sagemaker.training_runtime import TrainingRuntimeError, _publish_output_data, _weather_aware_test_metrics, build_command, trainer_script


def test_training_dispatcher_uses_reviewed_weather_aware_trainers(tmp_path: Path) -> None:
    gold = tmp_path / "gold.parquet"
    gold.write_bytes(b"fixture")
    weather = tmp_path / "weather"
    weather.mkdir()
    command = build_command(
        model="xgboost", gold_path=gold, weather_roots=[weather], output_dir=tmp_path / "out",
        split_mode="chronological", train_target_end="2024-12-31T23:00:00Z",
        validation_target_end="2025-06-30T23:00:00Z", seed=7, context_hours=168, epochs=12,
    )
    assert trainer_script("xgboost").name == "train_weather_aware_xgboost_curve.py"
    assert "--weather-root" in command
    assert "--split-mode" in command and "chronological" in command
    bilstm = build_command(
        model="bilstm", gold_path=gold, weather_roots=[weather], output_dir=tmp_path / "bilstm-out",
        split_mode="chronological", train_target_end="2024-12-31T23:00:00Z",
        validation_target_end="2025-06-30T23:00:00Z", seed=7, context_hours=168, epochs=1,
    )
    assert bilstm[-2:] == ["--device", "cuda"]


def test_training_dispatcher_refuses_noncausal_or_chronos_training(tmp_path: Path) -> None:
    gold = tmp_path / "gold.parquet"
    gold.write_bytes(b"fixture")
    weather = tmp_path / "weather"
    weather.mkdir()
    with pytest.raises(TrainingRuntimeError, match="Chronos-2"):
        trainer_script("chronos2")
    with pytest.raises(TrainingRuntimeError, match="chronological"):
        build_command(
            model="bilstm", gold_path=gold, weather_roots=[weather], output_dir=tmp_path / "out",
            split_mode="demo", train_target_end="2024-12-31T23:00:00Z",
        validation_target_end="2025-06-30T23:00:00Z", seed=7, context_hours=168, epochs=1,
        )


def test_output_data_summary_keeps_control_plane_facts_pending(tmp_path: Path) -> None:
    target = tmp_path / "model" / "candidate"; target.mkdir(parents=True)
    for name in ("metrics.json", "model_contract.json"):
        (target / name).write_text(json.dumps({"metrics": {}}), encoding="utf-8")
    lineage_file = target.parent / "training_lineage.json"
    lineage_file.write_text("{}", encoding="utf-8")
    lineage = {"training_job_name": "tei-test", "model": "bilstm", "experiment_name": "experiment", "dataset_manifest_uri": "s3://bucket/manifest/", "dataset_manifest_sha256": "a" * 64, "git_revision": "b" * 40, "training_mode": "curve", "split_mode": "chronological", "seed": 42, "context_hours": 168, "files_sha256": {}}
    _publish_output_data(output_data_dir=tmp_path / "output", lineage=lineage, metrics={"metrics": {}}, target=target)
    summary = json.loads((tmp_path / "output" / "training_summary.json").read_text())
    assert summary["training_job_arn"] is None
    assert "TrainingJobStatus" in summary["control_plane_metadata_pending"]


def test_final_metric_emission_accepts_bilstm_and_xgboost_schemas() -> None:
    values = {"mae_mw": 1.0, "rmse_mw": 2.0, "bias_mw": -3.0}
    xgboost = {"metrics": {"weather_aware": {"metrics": {"test": {"model": values}}}}}
    bilstm = {"metrics": {"weather_aware": {"test": {"model": values}}}}
    assert _weather_aware_test_metrics(xgboost) == values
    assert _weather_aware_test_metrics(bilstm) == values
