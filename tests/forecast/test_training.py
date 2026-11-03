from datetime import datetime, timedelta, timezone

import pytest

from texas_energy.forecast.service import ForecastService
from texas_energy.forecast.training import fit_hour_of_week_mean, predict_origins, synthetic_hourly_demand
from texas_energy.ml.temporal import ChronologicalSplit, build_forecast_origins


def _split(values: dict[datetime, float]) -> ChronologicalSplit:
    timestamps = tuple(values)
    return ChronologicalSplit(timestamps[39], timestamps[55], timestamps[-1])


def test_training_uses_only_chronological_train_values_and_saves_artifact(tmp_path) -> None:
    values = synthetic_hourly_demand(start_utc=datetime(2020, 1, 1, tzinfo=timezone.utc), hours=72)
    model = fit_hour_of_week_mean(values, split=_split(values))
    path = tmp_path / "model.json"
    model.save(path)

    assert path.is_file()
    assert model.predict(datetime(2020, 1, 4, 12, tzinfo=timezone.utc)) > 0


def test_predictions_and_service_enforce_horizon_and_target_alignment() -> None:
    values = synthetic_hourly_demand(start_utc=datetime(2020, 1, 1, tzinfo=timezone.utc), hours=72)
    split = _split(values)
    model = fit_hour_of_week_mean(values, split=split)
    origins = build_forecast_origins(tuple(values), horizon_hours=1, context_hours=24)
    predictions = predict_origins(
        model=model, model_name="smoke", origins=origins, values_by_timestamp=values, split=split
    )
    assert predictions[0].target_utc == predictions[0].origin_utc + timedelta(hours=1)
    service = ForecastService({"bilstm_v2": model})
    result = service.forecast(model_name="bilstm_v2", origin_utc=predictions[0].origin_utc, horizon_hours=1)
    assert result.target_utc == predictions[0].target_utc
    with pytest.raises(ValueError, match="horizon"):
        service.forecast(model_name="bilstm_v2", origin_utc=predictions[0].origin_utc, horizon_hours=24)
