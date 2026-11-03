from texas_energy.forecast.evaluation import EvaluationMetrics


def test_tracking_module_is_importable_without_importing_mlflow() -> None:
    from texas_energy.forecast.tracking import log_local_experiment

    assert callable(log_local_experiment)
    assert EvaluationMetrics(1, 1.0, 1.0).mae_mw == 1.0
