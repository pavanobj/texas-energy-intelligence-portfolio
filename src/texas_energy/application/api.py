import hashlib, importlib.util, json, os, sqlite3, threading, time, uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
import numpy as np
import pandas as pd
UTC=timezone.utc
MODELS={"xgboost_curve_demand_only","xgboost_curve_weather_aware","bilstm_curve_demand_only","bilstm_curve_weather_aware","amazon/chronos-2"}
class ReplayError(ValueError):pass
@dataclass(frozen=True)
class Settings:
 root:Path; catalog:Path; database:Path; gold:Path; weather_roots:tuple[Path,...]; device:str="cuda"; lag:int=24
 @classmethod
 def local(cls):
  root=Path(os.getenv("TEXAS_ENERGY_DATA_ROOT","data/texas-energy")); close=Path(os.getenv("TEXAS_ENERGY_CLOSEOUT_ROOT",str(root/"final-closeout")))
  return cls(root,close/"model_catalog.json",Path(os.getenv("TEXAS_ENERGY_FORECAST_DB",str(close/"api/forecasts.sqlite"))),Path(os.getenv("TEXAS_ENERGY_GOLD_PATH",str(root/"gold-candidate"))),(root/"open-meteo-ifs-daily-31day-demo-20261019",root/"open-meteo-ifs-daily-full-20261019"),os.getenv("TEXAS_ENERGY_DEVICE","cuda"))
def utc(v):
 if v.tzinfo is None or v.utcoffset()!=timedelta(0):raise ReplayError("forecast_origin_utc must be timezone-aware UTC")
 return v.astimezone(UTC)
def mod(name,file):
 s=importlib.util.spec_from_file_location(name,Path(__file__).resolve().parents[3]/"scripts"/file);m=importlib.util.module_from_spec(s);s.loader.exec_module(m);return m
class Store:
 def __init__(self,p):
  self.p=p;p.parent.mkdir(parents=True,exist_ok=True)
  with sqlite3.connect(p) as c:c.execute("CREATE TABLE IF NOT EXISTS forecast(id TEXT PRIMARY KEY,fp TEXT UNIQUE,payload TEXT)")
 def save(self,fp,x):
  with sqlite3.connect(self.p) as c:
   r=c.execute("SELECT payload FROM forecast WHERE fp=?",(fp,)).fetchone()
   if r:return json.loads(r[0]),True
   c.execute("INSERT INTO forecast VALUES(?,?,?)",(x["forecast_id"],fp,json.dumps(x,allow_nan=False)))
  return x,False
 def get(self,id):
  with sqlite3.connect(self.p) as c:r=c.execute("SELECT payload FROM forecast WHERE id=?",(id,)).fetchone()
  return json.loads(r[0]) if r else None
 def list(self,n,offset):
  with sqlite3.connect(self.p) as c:r=c.execute("SELECT payload FROM forecast ORDER BY rowid DESC LIMIT ? OFFSET ?",(n,offset)).fetchall()
  return [json.loads(x[0]) for x in r]
class Engine:
 def __init__(self,s):
  self.s=s;self.cat=json.loads(s.catalog.read_text());self.cache={};self.lock=threading.Lock();self.gpu=threading.Lock()
 def entry(self,id):
  for x in self.cat["models"]:
   if x["model_id"]==id:return x
  raise ReplayError("unknown model_id")
 def gold(self):
  if "gold" not in self.cache:
   x=mod("gold","train_xgboost_v2.py")._read_gold(self.s.gold);x["period_utc"]=pd.to_datetime(x.period_utc,utc=True);self.cache["gold"]=x
  return self.cache["gold"]
 def weather(self):
  if "weather" not in self.cache:
   x=mod("weather","train_weather_aware_bilstm_curve.py")._weather(self.s.weather_roots)
   for c in ("weather_available_at_utc","weather_model_run_utc","valid_time_utc"):x[c]=pd.to_datetime(x[c],utc=True)
   self.cache["weather"]=x
  return self.cache["weather"]
 def xgb_inputs(self):
  if "xgb_inputs" not in self.cache:
   from texas_energy.forecast.xgboost_curve import build_issued_curve_demand_examples
   from texas_energy.forecast.weather_features import add_point_in_time_weather_features
   t=mod("xt","train_weather_aware_xgboost_curve.py");d=build_issued_curve_demand_examples(self.gold()).examples;d=d.loc[d.forecast_origin_utc.isin(self.weather().weather_available_at_utc.unique())];self.cache["xgb_inputs"]=add_point_in_time_weather_features(d,self.weather(),expected_cities=t.CITIES).features
  return self.cache["xgb_inputs"]
 def bilstm_inputs(self):
  if "bilstm_inputs" not in self.cache:self.cache["bilstm_inputs"]=mod("bt","train_weather_aware_bilstm_curve.py")._dataset(self.gold(),self.weather())
  return self.cache["bilstm_inputs"]
 def load(self,id):
  if id in self.cache:return self.cache[id]
  if id.startswith("xgboost"):
   from xgboost import XGBRegressor;m=XGBRegressor();m.load_model(self.entry(id)["artifact_path"]);v=m
  elif id.startswith("bilstm"):
   import torch;p=json.loads((self.s.catalog.parent/"bilstm/preprocessing.json").read_text());q=mod("bm","train_weather_aware_bilstm_curve.py");m=q._model(torch,len(p["weather_columns"]),id.endswith("weather_aware"));m.load_state_dict(torch.load(self.entry(id)["artifact_path"],map_location="cpu",weights_only=True)["state_dict"]);dev="cuda" if self.s.device=="cuda" and torch.cuda.is_available() else "cpu";m.to(dev).eval();v=(m,p,dev)
  else:
   import torch
   from chronos import Chronos2Pipeline
   dev="cuda" if self.s.device=="cuda" and torch.cuda.is_available() else "cpu";v=(Chronos2Pipeline.from_pretrained("amazon/chronos-2",device_map=dev,torch_dtype=torch.float32),dev)
  self.cache[id]=v;return v
 def replay(self,id,o):
  if id not in MODELS:raise ReplayError("model does not support 24-hour replay")
  o=utc(o);start=time.monotonic()
  if id.startswith("xgboost"):
   r=self.xgb_inputs().loc[lambda x:x.forecast_origin_utc.eq(pd.Timestamp(o))].sort_values("horizon_hours")
   if len(r)!=24:raise ReplayError("incomplete issued-weather archive for origin")
   c=json.loads((self.s.catalog.parent/"xgboost/model_contract.json").read_text());cols=c["weather_aware_features"] if id.endswith("weather_aware") else c["demand_only_features"];y=self.load(id).predict(r[cols]);ce=pd.Timestamp(r.context_end_utc.iloc[0]).to_pydatetime()
  elif id.startswith("bilstm"):
   import torch
   origins,d,w,_=self.bilstm_inputs()
   try:i=int(origins.get_loc(pd.Timestamp(o)))
   except KeyError:raise ReplayError("incomplete issued-weather archive for origin")
   m,p,dev=self.load(id);dn=(d[i:i+1]-p["demand_mean"])/p["demand_std"];wn=(w[i:i+1]-np.array(p["weather_mean"]))/np.where(np.array(p["weather_std"])==0,1,np.array(p["weather_std"]))
   with self.gpu,torch.inference_mode():y=m(torch.from_numpy(dn[:,:,None].astype(np.float32)).to(dev),torch.from_numpy(wn.astype(np.float32)).to(dev)).cpu().numpy()[0]
   y=y*p["target_std"]+p["target_mean"];ce=o-timedelta(hours=self.s.lag)
  else:
   from texas_energy.forecast.chronos2 import build_issued_curve_origins,issued_curve_contexts_to_long_frame,issued_curve_predictions
   z=build_issued_curve_origins(self.gold()[["period_utc","observed_average_demand_mw"]],forecast_start_utc=pd.Timestamp(o),forecast_end_utc=pd.Timestamp(o),context_hours=2048,horizon_hours=24,demand_availability_lag_hours=self.s.lag)
   if len(z)!=1:raise ReplayError("incomplete causal Chronos context")
   pipe,_=self.load(id)
   with self.gpu:r=pipe.predict_df(issued_curve_contexts_to_long_frame(z),id_column="item_id",timestamp_column="timestamp",target="target",prediction_length=48,quantile_levels=[0.5],batch_size=1,context_length=2048,freq="h")
   y=issued_curve_predictions(r,z).sort_values("horizon_hours").predicted_demand_mw.to_numpy();ce=o-timedelta(hours=self.s.lag)
  if len(y)!=24 or not np.isfinite(y).all():raise RuntimeError("invalid model curve")
  wr=self.weather().loc[lambda x:x.weather_available_at_utc.eq(pd.Timestamp(o))]
  return y,ce,{"demand_source":"accepted ERCOT Gold historical demand","weather_source":"ECMWF IFS issued forecast archive","weather_model_run_utc":None if wr.empty else wr.weather_model_run_utc.iloc[0].isoformat(),"weather_available_at_utc":o.isoformat(),"inference_duration_ms":round((time.monotonic()-start)*1000,2),"demand_availability_lag_hours":self.s.lag}
def create_app(settings=None):
 from fastapi import FastAPI,HTTPException,Query,Request
 from fastapi.responses import JSONResponse
 from pydantic import BaseModel,ConfigDict,field_validator
 s=settings or Settings.local();store=Store(s.database);engine=Engine(s);app=FastAPI(title="Texas Energy Intelligence API",version="2.0.0")
 class Replay(BaseModel):
  model_config=ConfigDict(extra="forbid")
  forecast_origin_utc:datetime;model_id:str|None=None;include_actuals:bool=True
  @field_validator("forecast_origin_utc")
  @classmethod
  def stamp(cls,v):return utc(v)
 def fail(c,k,m):raise HTTPException(c,{"category":k,"message":m})
 @app.exception_handler(Exception)
 async def safe(_,e):
  if isinstance(e,HTTPException):return JSONResponse(status_code=e.status_code,content={"detail":e.detail})
  return JSONResponse(status_code=500,content={"detail":{"category":"internal_error","message":"Request could not be completed safely."}})
 @app.get("/health")
 def health():return {"status":"ok","version":app.version,"timestamp_utc":datetime.now(UTC).isoformat(),"models_loaded":sorted(x for x in engine.cache if x in MODELS)}
 @app.get("/ready")
 def ready():
  id=engine.cat["selection"]["selected_default"];e=engine.entry(id);ok=Path(e["artifact_path"]).exists()
  return {"api_process_ready":True,"historical_replay_ready":ok,"selected_model_id":id,"selected_model_loadable":ok,"live_demand_source_ready":False,"live_weather_source_ready":False,"live_forecasting_ready":False,"live_blocker":"Operational demand and issued-weather providers are not configured."}
 @app.get("/models")
 def models():
  b=json.loads((s.catalog.parent/"comparison/leaderboard.json").read_text());scores={x["model_id"]:x for x in b if x["evaluation_split"]=="validation"};r=[]
  for e in engine.cat["models"]:
   x={k:v for k,v in e.items() if k!="artifact_path"};x["artifact_available"]=not e.get("artifact_path") or Path(e["artifact_path"]).exists();x["validation_metrics"]=scores.get(e["model_id"]);r.append(x)
  return {"models":r,"default_model_id":engine.cat["selection"]["selected_default"]}
 @app.get("/models/{model_id:path}")
 def model(model_id):
  try:e=engine.entry(model_id)
  except ReplayError as x:fail(404,"unknown_model",str(x))
  return {k:v for k,v in e.items() if k!="artifact_path"}|{"artifact_available":not e.get("artifact_path") or Path(e["artifact_path"]).exists()}
 @app.post("/forecasts/replay")
 def replay(q:Replay):
  id=q.model_id or engine.cat["selection"]["selected_default"]
  try:y,ce,p=engine.replay(id,q.forecast_origin_utc);a=engine.gold().set_index("period_utc").observed_average_demand_mw.reindex(pd.date_range(pd.Timestamp(q.forecast_origin_utc)+pd.Timedelta(hours=1),periods=24,freq="h",tz="UTC"))
  except ReplayError as x:fail(422,"replay_input_unavailable",str(x))
  except FileNotFoundError:fail(503,"artifact_unavailable","Required local artifact unavailable.")
  if a.isna().any():fail(422,"target_actual_unavailable","Incomplete target actuals.")
  rows=[{"horizon_hours":h,"target_timestamp_utc":(q.forecast_origin_utc+timedelta(hours=h)).isoformat(),"predicted_demand_mw":float(y[h-1]),"actual_demand_mw":float(a.iloc[h-1]) if q.include_actuals else None} for h in range(1,25)]
  peak=max(rows,key=lambda x:x["predicted_demand_mw"]);v={"forecast_id":str(uuid.uuid4()),"mode":"historical_replay","model_id":id,"forecast_origin_utc":q.forecast_origin_utc.isoformat(),"context_end_utc":ce.isoformat(),"horizon_hours":24,"unit":"MW","created_at_utc":datetime.now(UTC).isoformat(),"predictions":rows,"summary":{"predicted_peak_demand_mw":peak["predicted_demand_mw"],"predicted_peak_timestamp_utc":peak["target_timestamp_utc"],"predicted_average_demand_mw":float(np.mean(y))},"provenance":p,"data_quality_flags":["historical_replay","issued_weather_only","demand_latency_policy_applied"]}
  fp=hashlib.sha256(json.dumps({"model":id,"origin":v["forecast_origin_utc"],"actuals":q.include_actuals},sort_keys=True).encode()).hexdigest();x,cached=store.save(fp,v);x["cached"]=cached;return x
 @app.post("/forecasts/live")
 def live():fail(503,"live_inputs_not_ready","Live forecasting needs verified operational demand and issued-weather providers.")
 @app.get("/forecasts/{forecast_id}")
 def get(forecast_id):
  x=store.get(forecast_id)
  if not x:fail(404,"forecast_not_found","No saved forecast.")
  return x
 @app.get("/forecasts")
 def listing(limit:int=Query(20,ge=1,le=100),offset:int=Query(0,ge=0)):return {"items":store.list(limit,offset),"limit":limit,"offset":offset}
 @app.get("/forecast-performance")
 def performance(model_id:str|None=None,split:str|None=None,horizon_hours:int|None=Query(None,ge=1,le=24)):
  r=json.loads((s.catalog.parent/"comparison"/("per_horizon_leaderboard.json" if horizon_hours else "leaderboard.json")).read_text());return {"cohort":"strict_common_origin","rows":[x for x in r if(not model_id or x["model_id"]==model_id)and(not split or x["evaluation_split"]==split)and(horizon_hours is None or x.get("horizon_hours")==horizon_hours)]}
 @app.get("/demand/history")
 def demand(start_utc:datetime,end_utc:datetime):
  try:a,b=utc(start_utc),utc(end_utc)
  except ReplayError as x:fail(422,"invalid_datetime",str(x))
  if b<a or b-a>timedelta(days=31):fail(422,"range_invalid","Maximum request range is 31 days.")
  d=engine.gold().loc[lambda x:x.period_utc.between(pd.Timestamp(a),pd.Timestamp(b))];return {"records":[{"timestamp_utc":x.period_utc.isoformat(),"average_demand_mw":float(x.observed_average_demand_mw),"unit":"MW","source":"ERCOT historical Gold"} for x in d.itertuples()]}
 @app.get("/weather/forecast")
 def weather(forecast_origin_utc:datetime):
  try:o=utc(forecast_origin_utc)
  except ReplayError as x:fail(422,"invalid_datetime",str(x))
  w=engine.weather().loc[lambda x:x.weather_available_at_utc.eq(pd.Timestamp(o))]
  if w.empty:fail(404,"weather_forecast_unavailable","No archived issued-weather run.")
  return {"mode":"historical_issued_weather_forecast","forecast_origin_utc":o.isoformat(),"weather_model_run_utc":w.weather_model_run_utc.iloc[0].isoformat(),"records":json.loads(w.sort_values(["valid_time_utc","city"]).head(120).to_json(orient="records",date_format="iso"))}
 @app.get("/forecast-summary/{forecast_id}")
 def summary(forecast_id):
  f=store.get(forecast_id)
  if not f:fail(404,"forecast_not_found","No saved forecast.")
  p=f["predictions"];lo=min(p,key=lambda x:x["predicted_demand_mw"]);hi=max(p,key=lambda x:x["predicted_demand_mw"])
  return {"forecast_id":forecast_id,"model_id":f["model_id"],"mode":f["mode"],"predicted_average_demand_mw":f["summary"]["predicted_average_demand_mw"],"predicted_minimum":lo,"predicted_maximum":hi,"hourly_changes_mw":[p[i]["predicted_demand_mw"]-p[i-1]["predicted_demand_mw"] for i in range(1,24)],"warnings":f["data_quality_flags"]}
 return app
