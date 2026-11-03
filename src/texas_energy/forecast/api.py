"""Minimal FastAPI contract; model loading and serving remain deliberately external."""

# Do not enable postponed annotations here: FastAPI must receive the actual
# function-local Pydantic request class when it constructs the /forecast body.


def create_app(*, service=None):
    """Create a dependency-light API exposing model metadata and validated forecast input."""

    try:
        from fastapi import FastAPI
        from pydantic import BaseModel, Field
    except ImportError:
        raise RuntimeError("Install the forecast optional dependency group to enable the FastAPI contract.") from None

    class ForecastRequest(BaseModel):
        model_name: str
        horizon_hours: int = Field(ge=1, le=168)
        origin_utc: str

    app = FastAPI(title="Texas Energy Forecast API", version="0.1.0")

    @app.get("/models")
    def models() -> dict[str, object]:
        from .specification import MODEL_SPECS

        return {name: {"horizon_hours": spec.horizon_hours, "context_hours": spec.context_hours} for name, spec in MODEL_SPECS.items()}

    @app.post("/forecast")
    def forecast(request: ForecastRequest) -> dict[str, object]:
        if service is None:
            return {"model_name": request.model_name, "horizon_hours": request.horizon_hours, "status": "model_not_loaded"}
        from datetime import datetime

        result = service.forecast(
            model_name=request.model_name,
            horizon_hours=request.horizon_hours,
            origin_utc=datetime.fromisoformat(request.origin_utc.replace("Z", "+00:00")),
        )
        return {
            "model_name": result.model_name,
            "origin_utc": result.origin_utc.isoformat(),
            "target_utc": result.target_utc.isoformat(),
            "horizon_hours": result.horizon_hours,
            "prediction_mw": result.prediction_mw,
        }

    return app
