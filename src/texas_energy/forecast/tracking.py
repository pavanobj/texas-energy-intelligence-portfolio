"""Opt-in local MLflow tracking; importing this module does not require MLflow."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from pathlib import Path

from .evaluation import EvaluationMetrics


def log_local_experiment(
    *,
    experiment_name: str,
    run_name: str,
    parameters: Mapping[str, str | int | float],
    metrics: EvaluationMetrics,
    artifact_location: str | None = None,
    artifacts: Sequence[Path] = (),
) -> None:
    """Log a completed local evaluation to an explicit local MLflow experiment.

    ``artifact_location`` is only used when creating the experiment.  Passing
    an explicit SQLite tracking URI through ``MLFLOW_TRACKING_URI`` avoids
    MLflow 3's deprecated file-store backend.
    """

    try:
        import mlflow
    except ImportError:
        raise RuntimeError("Install the forecast optional dependency group to enable MLflow tracking.") from None
    existing = mlflow.get_experiment_by_name(experiment_name)
    if existing is None and artifact_location is not None:
        mlflow.create_experiment(experiment_name, artifact_location=artifact_location)
    mlflow.set_experiment(experiment_name)
    with mlflow.start_run(run_name=run_name):
        mlflow.log_params(dict(parameters))
        mlflow.log_metrics({"count": metrics.count, "mae_mw": metrics.mae_mw, "rmse_mw": metrics.rmse_mw})
        for artifact in artifacts:
            mlflow.log_artifact(str(artifact))
