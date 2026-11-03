"""One batch dispatcher for XGBoost, Bi-LSTM, and Chronos-2 24-hour forecasts."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

HORIZON_HOURS = 24
CONTEXT_HOURS = 168
MODELS = ("xgboost", "bilstm", "chronos2")


def arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", choices=MODELS, required=True)
    parser.add_argument("--gold", type=Path, default=Path("/opt/ml/input/data/training/gold.parquet"))
    parser.add_argument("--model-dir", type=Path, default=Path(os.environ.get("SM_MODEL_DIR", "/opt/ml/model")))
    parser.add_argument("--output-dir", type=Path, default=Path(os.environ.get("SM_OUTPUT_DATA_DIR", "/opt/ml/output/data")))
    parser.add_argument("--run-id", default=os.environ.get("SM_TRAINING_JOB_NAME", "local"))
    parser.add_argument("--validation-only", action="store_true")
    return parser.parse_args()


def load_gold(path: Path) -> pd.DataFrame:
    if not path.is_file():
        raise RuntimeError(f"Gold input does not exist: {path}")
    frame = pd.read_parquet(path)
    missing = {"period_utc", "observed_average_demand_mw"}.difference(frame.columns)
    if missing:
        raise RuntimeError(f"Gold input is missing required columns: {sorted(missing)}")
    frame = frame.loc[:, ["period_utc", "observed_average_demand_mw"]].copy()
    frame["period_utc"] = pd.to_datetime(frame["period_utc"], utc=True)
    frame["observed_average_demand_mw"] = pd.to_numeric(frame["observed_average_demand_mw"], errors="raise")
    frame = frame.sort_values("period_utc").drop_duplicates("period_utc", keep=False).reset_index(drop=True)
    if frame.empty or not np.isfinite(frame["observed_average_demand_mw"]).all():
        raise RuntimeError("Gold demand must contain finite, unique hourly MW values.")
    if not frame["period_utc"].diff().dropna().eq(pd.Timedelta(hours=1)).all():
        raise RuntimeError("Gold snapshot has a missing/irregular hour; refusing compressed contexts.")
    return frame


def training_arrays(gold: pd.DataFrame) -> tuple[np.ndarray, np.ndarray]:
    values = gold["observed_average_demand_mw"].to_numpy(np.float32)
    if len(values) < CONTEXT_HOURS + HORIZON_HOURS + 1:
        raise RuntimeError("Gold lacks enough contiguous demand for 168-hour contexts and 24-hour curves.")
    return (
        np.asarray([values[end - CONTEXT_HOURS : end] for end in range(CONTEXT_HOURS, len(values) - HORIZON_HOURS + 1)]),
        np.asarray([values[end : end + HORIZON_HOURS] for end in range(CONTEXT_HOURS, len(values) - HORIZON_HOURS + 1)]),
    )


def xgboost_curve(contexts: np.ndarray, targets: np.ndarray, model_dir: Path) -> np.ndarray:
    from xgboost import XGBRegressor
    model = XGBRegressor(n_estimators=250, max_depth=6, learning_rate=0.05, objective="reg:squarederror", n_jobs=1)
    model.fit(contexts, targets); model_dir.mkdir(parents=True, exist_ok=True)
    model.save_model(str(model_dir / "xgboost-24h.json"))
    return model.predict(contexts[-1:])[0]


def bilstm_curve(contexts: np.ndarray, targets: np.ndarray, model_dir: Path) -> np.ndarray:
    import torch
    torch.manual_seed(42); mean, std = float(contexts.mean()), float(contexts.std() or 1.0)
    class Curve(torch.nn.Module):
        def __init__(self) -> None:
            super().__init__(); self.first=torch.nn.LSTM(1,128,batch_first=True,bidirectional=True); self.second=torch.nn.LSTM(256,64,batch_first=True,bidirectional=True); self.head=torch.nn.Sequential(torch.nn.Linear(128,64),torch.nn.ReLU(),torch.nn.Linear(64,HORIZON_HOURS))
        def forward(self, x: torch.Tensor) -> torch.Tensor:
            x,_=self.first(x); x,_=self.second(x); return self.head(x[:,-1])
    model=Curve(); x=torch.from_numpy(((contexts-mean)/std)[:,:,None]); y=torch.from_numpy((targets-mean)/std); optimizer=torch.optim.Adam(model.parameters(),lr=1e-3)
    for _ in range(3):
        optimizer.zero_grad(set_to_none=True); loss=torch.nn.functional.mse_loss(model(x),y); loss.backward(); optimizer.step()
    model_dir.mkdir(parents=True,exist_ok=True); torch.save({"state_dict":model.state_dict(),"mean":mean,"std":std,"architecture":"BiLSTM-128-64-direct-24"},model_dir/"bilstm-24h.pt")
    with torch.inference_mode(): return (model(x[-1:])[0].numpy()*std+mean).astype(np.float32)


def chronos_curve(contexts: np.ndarray) -> np.ndarray:
    import torch
    from chronos import Chronos2Pipeline
    pipeline=Chronos2Pipeline.from_pretrained("amazon/chronos-2",device_map="cpu",torch_dtype=torch.float32)
    samples=pipeline.predict(context=torch.from_numpy(contexts[-1:]),prediction_length=HORIZON_HOURS).detach().cpu().numpy()
    return np.median(samples[0],axis=0).astype(np.float32)


def execute(args: argparse.Namespace) -> dict[str, object]:
    gold=load_gold(args.gold); contexts,targets=training_arrays(gold)
    if args.validation_only:
        return {"status":"validated","model":args.model,"examples":int(len(contexts)),"horizon_hours":HORIZON_HOURS}
    prediction={"xgboost":lambda: xgboost_curve(contexts,targets,args.model_dir),"bilstm":lambda: bilstm_curve(contexts,targets,args.model_dir),"chronos2":lambda: chronos_curve(contexts)}[args.model]()
    if prediction.shape != (HORIZON_HOURS,) or not np.isfinite(prediction).all(): raise RuntimeError("Model did not emit 24 finite hourly MW predictions.")
    origin=gold["period_utc"].iloc[-1]; args.output_dir.mkdir(parents=True,exist_ok=True)
    pd.DataFrame({"model_id":args.model,"run_id":args.run_id,"forecast_origin_utc":origin,"target_timestamp_utc":origin+pd.to_timedelta(range(1,25),unit="h"),"horizon_hours":range(1,25),"predicted_demand_mw":prediction,"unit":"MW","forecast_generation_type":"batch_gold_snapshot"}).to_parquet(args.output_dir/"predictions.parquet",index=False)
    lineage={"schema_version":"sagemaker-forecast-v1","model":args.model,"run_id":args.run_id,"gold_snapshot_sha256":hashlib.sha256(args.gold.read_bytes()).hexdigest(),"gold_latest_timestamp_utc":origin.isoformat(),"context_hours":CONTEXT_HOURS,"horizon_hours":HORIZON_HOURS,"unit":"MW","mode":"pretrained_zero_shot" if args.model=="chronos2" else "trained","created_at_utc":datetime.now(timezone.utc).isoformat()}
    (args.output_dir/"lineage.json").write_text(json.dumps(lineage,indent=2),encoding="utf-8")
    return {"status":"completed","rows":HORIZON_HOURS,**lineage}


def _legacy_main() -> int:
    print(json.dumps(execute(arguments()),sort_keys=True)); return 0

# The former demand-only scaffold remains only as an import-compatible source
# history while this public module delegates all callers to the artifact-backed
# runtime.  Keeping this shim avoids a second SageMaker entrypoint.
from texas_energy.sagemaker.unified_runtime import (  # noqa: E402
    HORIZON_HOURS,
    MODELS,
    SageMakerRuntimeError,
    arguments,
    artifact_provenance,
    bilstm_curve,
    chronos2_curve,
    execute,
    main,
    xgboost_curve,
)

if __name__ == "__main__": raise SystemExit(main())
