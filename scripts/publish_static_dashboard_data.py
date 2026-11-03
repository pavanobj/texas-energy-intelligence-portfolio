"""Publish validated historical forecasting artifacts as safe static JSON."""
from __future__ import annotations
import argparse,json,math,os,re
from datetime import datetime,timezone
from numbers import Number
from pathlib import Path
import pandas as pd
# Public payloads may include paths inside nested JSON values, so do not anchor
# this check at the beginning of the serialized document.
ABS=re.compile(r"(?:[A-Za-z]:[\\/]|/(?:home|mnt)/)")
class PublishError(ValueError):pass
MODEL_LABELS={
 "weather_aware_xgboost_curve":"Weather-aware XGBoost",
 "weather_aware_bilstm_curve":"Weather-aware Bi-LSTM",
 "amazon/chronos-2":"Chronos-2 zero-shot",
}
def atomic(path,obj):
 path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(".tmp")
 tmp.write_text(json.dumps(obj,indent=2,allow_nan=False)+"\n",encoding="utf-8"); tmp.replace(path)
def finite(v):
 if not isinstance(v,Number) or not math.isfinite(float(v)):raise PublishError("Non-finite public numeric value.")
 return float(v)
def finite_or_pending(v):
 """Map a genuinely unavailable Gold value to the public pending representation."""
 if v is None or pd.isna(v):return None
 return finite(v)
def public(obj):
 text=json.dumps(obj,allow_nan=False)
 if ABS.search(text) or re.search(r"(?i)(api[_-]?key|authorization|secret)",text):raise PublishError("Public payload contains an unsafe path or credential-like field.")
 return obj
def validate_document(name,obj):
 if obj.get("schema_version")!="1.0":raise PublishError(f"{name} has an unsupported schema version.")
 if name=="latest-forecast.json":
  if obj.get("status")!="historical_replay" or obj.get("historical_not_live") is not True or obj.get("unit")!="MW":raise PublishError("Forecast status, unit, or historical label is invalid.")
  rows=obj.get("predictions")
  if not isinstance(rows,list) or len(rows)!=24:raise PublishError("Forecast must contain exactly 24 prediction rows.")
  origin=pd.to_datetime(obj.get("forecast_origin_utc"),utc=True)
  for h,row in enumerate(rows,1):
   if row.get("horizon_hours")!=h or pd.to_datetime(row.get("target_timestamp_utc"),utc=True)!=origin+pd.Timedelta(hours=h):raise PublishError("Forecast horizon continuity is invalid.")
   for key in ("actual_demand_mw","xgboost_weather_aware_mw","bilstm_weather_aware_mw","chronos2_mw"):finite(row.get(key))
 elif name=="demand-history.json":
  if obj.get("status")!="historical_replay_actuals" or obj.get("historical_not_live") is not True or obj.get("unit")!="MW":raise PublishError("Demand history label or unit is invalid.")
  for row in obj.get("records",[]):
   pd.to_datetime(row.get("timestamp_utc"),utc=True);finite(row.get("actual_demand_mw"))
 elif name=="weather-summary.json":
  if obj.get("status")!="historical_issued_weather_metadata_only" or obj.get("historical_not_live") is not True or obj.get("weather_values_available") is not False:raise PublishError("Weather summary historical label is invalid.")
 elif name=="model-performance.json":
  if obj.get("status")!="historical_evaluation" or not isinstance(obj.get("leaderboard"),list) or not isinstance(obj.get("per_horizon"),list):raise PublishError("Model performance schema is invalid.")
 elif name=="forecast-metadata.json":
  if obj.get("historical_not_live") is not True or not isinstance(obj.get("source_artifacts"),list):raise PublishError("Forecast metadata schema is invalid.")
 elif name=="prospective-forecast.json":
  if obj.get("status")!="prospective_forecast_retrospective_evaluation" or obj.get("forecast_generation_type")!="prospective_live_inference":raise PublishError("Prospective forecast status is invalid.")
  if obj.get("not_current_live_forecast") is not True or obj.get("unit")!="MW":raise PublishError("Prospective forecast disclosure or unit is invalid.")
  origin=pd.to_datetime(obj.get("forecast_origin_utc"),utc=True)
  coverage=obj.get("actual_coverage",{})
  if coverage.get("total_target_hours")!=24 or not isinstance(obj.get("models"),list):raise PublishError("Prospective forecast coverage is invalid.")
  for model in obj["models"]:
   rows=model.get("predictions")
   if model.get("variant") not in {"raw","calibrated"} or not isinstance(rows,list) or len(rows)!=24:raise PublishError("Prospective model rows are invalid.")
   for h,row in enumerate(rows,1):
    if row.get("horizon_hours")!=h or pd.to_datetime(row.get("target_timestamp_utc"),utc=True)!=origin+pd.Timedelta(hours=h):raise PublishError("Prospective forecast horizon continuity is invalid.")
    finite(row.get("predicted_demand_mw"))
    if row.get("actual_demand_mw") is not None:finite(row["actual_demand_mw"])
 elif name=="rolling-historical-replay.json":
  if obj.get("status")!="historical_rolling_backtest" or obj.get("historical_not_live") is not True or obj.get("unit")!="MW":raise PublishError("Rolling replay status or unit is invalid.")
  if obj.get("issue_count")!=14 or len(obj.get("curves",[]))!=336:raise PublishError("Rolling replay does not contain the expected 14 issued 24-hour curves.")
  if set(obj.get("metrics",{}))!={"7_day","14_day"}:raise PublishError("Rolling replay metrics are incomplete.")
  for row in obj["curves"]:
   if row.get("horizon_hours") not in range(1,25):raise PublishError("Rolling replay horizon is invalid.")
   pd.to_datetime(row.get("issue_timestamp_utc"),utc=True);pd.to_datetime(row.get("target_timestamp_utc"),utc=True)
   for key in ("xgboost_mw","bilstm_mw","chronos2_mw"):finite(row.get(key))
   finite_or_pending(row.get("actual_demand_mw"))
  for span in obj["metrics"].values():
   for value in span.values():
    if value["matched_observation_count"] <= 0:raise PublishError("Rolling replay has no genuine actuals.")
 else:raise PublishError(f"Unknown public document {name}.")
 return obj

def _iso(value):
 return pd.to_datetime(value,utc=True).isoformat()

def _require(frame,columns,label):
 missing=[column for column in columns if column not in frame.columns]
 if missing:raise PublishError(f"{label} is missing required columns: {', '.join(missing)}")

def _metrics(rows):
 matched=[row for row in rows if row["actual_demand_mw"] is not None]
 if not matched:return {"matched_observation_count":0,"mae_mw":None,"rmse_mw":None,"bias_mw":None,"maximum_absolute_error_mw":None}
 actual=[row["actual_demand_mw"] for row in matched];predicted=[row["predicted_demand_mw"] for row in matched]
 errors=[pred-act for pred,act in zip(predicted,actual)]
 return {"matched_observation_count":len(matched),"mae_mw":sum(abs(error) for error in errors)/len(errors),"rmse_mw":(sum(error**2 for error in errors)/len(errors))**.5,"bias_mw":sum(errors)/len(errors),"maximum_absolute_error_mw":max(abs(error) for error in errors)}

def _active_delta_files(root):
 """Resolve the committed Delta snapshot without treating old parquet files as current."""
 logs=sorted((root/"_delta_log").glob("*.json"))
 if not logs:raise PublishError("Gold Delta transaction log is unavailable.")
 active={}
 for log in logs:
  for line in log.read_text(encoding="utf-8").splitlines():
   entry=json.loads(line)
   if "add" in entry:active[entry["add"]["path"]]=entry["add"]
   elif "remove" in entry:active.pop(entry["remove"]["path"],None)
 files=[root/path for path in sorted(active)]
 if not files or any(not path.is_file() for path in files):raise PublishError("Gold Delta snapshot references unavailable parquet files.")
 return files

def _read_active_delta(root):
 return pd.concat([pd.read_parquet(path) for path in _active_delta_files(root)],ignore_index=True)

def _gold_actuals(root):
 frame=_read_active_delta(root).copy()
 _require(frame,["period_utc","observed_average_demand_mw","respondent","demand_unit"],"Gold demand actuals")
 return frame.rename(columns={"demand_unit":"value-units"})[["period_utc","observed_average_demand_mw","respondent","value-units"]]

def _observed_weather(root,targets):
 frame=_read_active_delta(root).copy()
 _require(frame,["period_utc","city","source","dataset","temperature_2m","relative_humidity_2m","wind_speed_10m","temperature_unit","relative_humidity_unit","wind_speed_unit","ingested_at_utc"],"Gold observed weather")
 frame["period_utc"]=pd.to_datetime(frame["period_utc"],utc=True)
 frame=frame[frame.period_utc.isin(pd.to_datetime(sorted(set(targets)),utc=True))].copy()
 if frame.duplicated(["city","period_utc"]).any():raise PublishError("Gold observed weather contains duplicate city-hour records.")
 records=[]
 for row in frame.sort_values(["city","period_utc"]).itertuples():
  records.append({"city":str(row.city).replace("_"," ").title(),"target_timestamp_utc":_iso(row.period_utc),"source":str(row.source),"dataset":str(row.dataset),"observed_at_utc":_iso(row.ingested_at_utc),"temperature_2m_c":finite(row.temperature_2m),"relative_humidity_pct":finite(row.relative_humidity_2m),"wind_speed_10m_kmh":finite(row.wind_speed_10m),"temperature_unit":str(row.temperature_unit),"relative_humidity_unit":str(row.relative_humidity_unit),"wind_speed_unit":str(row.wind_speed_unit)})
 return records

def publish_prospective(root,actuals_path,issue,output_dir,observed_weather_root=None):
 """Publish one immutable prospective issuance without exposing local paths."""
 issue_dir=root/"outputs"/f"issue={issue}"
 input_dir=root/"inputs"/f"issue={issue}"
 manifest_path=issue_dir/"issue_manifest.json"
 prediction_path=issue_dir/"forecast_predictions.parquet"
 weather_path=input_dir/"weather_forecasts.parquet"
 for path in (manifest_path,prediction_path,weather_path,actuals_path):
  if not path.is_file():raise PublishError(f"Required prospective artifact is unavailable: {path.name}")
 manifest=json.loads(manifest_path.read_text(encoding="utf-8"))
 if manifest.get("status")!="complete" or manifest.get("generation_type")!="prospective_live_inference":raise PublishError("Issue manifest is not a completed prospective issuance.")
 origin=pd.to_datetime(manifest.get("forecast_origin_utc"),utc=True)
 predictions=pd.read_parquet(prediction_path).copy()
 _require(predictions,["model_id","horizon_hours","target_timestamp_utc","forecast_origin_utc","predicted_demand_mw","demand_unit"],"prospective predictions")
 if set(predictions["demand_unit"].astype(str))!={"MW"}:raise PublishError("Prospective predictions do not declare MW.")
 predictions["target_timestamp_utc"]=pd.to_datetime(predictions["target_timestamp_utc"],utc=True)
 predictions["forecast_origin_utc"]=pd.to_datetime(predictions["forecast_origin_utc"],utc=True)
 if not predictions["forecast_origin_utc"].eq(origin).all():raise PublishError("Prospective prediction origins do not match the manifest.")
 actuals=pd.read_parquet(actuals_path).copy()
 _require(actuals,["period_utc","observed_average_demand_mw","respondent","value-units"],"EIA actuals")
 actuals["period_utc"]=pd.to_datetime(actuals["period_utc"],utc=True)
 if set(actuals["respondent"].astype(str))!={"ERCO"} or set(actuals["value-units"].astype(str).str.lower())!={"megawatthours"}:raise PublishError("Actuals are not ERCO hourly megawatthours.")
 if actuals.period_utc.duplicated().any():raise PublishError("Actual demand contains duplicate timestamps.")
 actual_by_timestamp={_iso(row.period_utc):finite_or_pending(row.observed_average_demand_mw) for row in actuals.itertuples()}
 models=[];targets=[]
 for model_id,group in predictions.groupby("model_id",sort=True):
  group=group.sort_values("horizon_hours")
  if list(group["horizon_hours"])!=list(range(1,25)):raise PublishError(f"{model_id} does not contain horizons 1 through 24.")
  if group.target_timestamp_utc.duplicated().any():raise PublishError(f"{model_id} has duplicate targets.")
  rows=[]
  for row in group.itertuples():
   expected=origin+pd.Timedelta(hours=int(row.horizon_hours))
   if row.target_timestamp_utc!=expected:raise PublishError(f"{model_id} target timestamp is misaligned.")
   target=_iso(row.target_timestamp_utc);targets.append(target)
   rows.append({"horizon_hours":int(row.horizon_hours),"target_timestamp_utc":target,"predicted_demand_mw":finite(row.predicted_demand_mw),"actual_demand_mw":actual_by_timestamp.get(target)})
  models.append({"model_id":str(model_id),"model_label":MODEL_LABELS.get(str(model_id),str(model_id)),"variant":"raw","calibration":{"applied":False,"reason":"No calibrated output was present in this immutable issuance."},"metrics":_metrics(rows),"predictions":rows})
 if len(models)!=3 or len(set(targets))!=24:raise PublishError("Prospective issue must contain three complete models over one shared 24-hour target set.")
 weather=pd.read_parquet(weather_path).copy()
 _require(weather,["city","valid_time_utc","weather_model_run_utc","weather_available_at_utc","temperature_2m_c","relative_humidity_pct","wind_speed_10m_kmh"],"issued weather")
 _require(pd.DataFrame([manifest]),["weather_model"],"issue manifest")
 weather["valid_time_utc"]=pd.to_datetime(weather["valid_time_utc"],utc=True)
 weather=weather[weather.valid_time_utc.isin(pd.to_datetime(sorted(set(targets)),utc=True))].copy()
 if len(weather)!=120 or weather.duplicated(["city","valid_time_utc"]).any() or weather.city.nunique()!=5:raise PublishError("Issued weather does not cover five cities for all 24 forecast targets.")
 summaries=[]
 for city,group in weather.groupby("city",sort=True):
  summaries.append({"city":str(city).replace("_"," ").title(),"temperature_2m_c":{"min":finite(group.temperature_2m_c.min()),"max":finite(group.temperature_2m_c.max()),"mean":finite(group.temperature_2m_c.mean())},"relative_humidity_pct":{"min":finite(group.relative_humidity_pct.min()),"max":finite(group.relative_humidity_pct.max()),"mean":finite(group.relative_humidity_pct.mean())},"wind_speed_10m_kmh":{"min":finite(group.wind_speed_10m_kmh.min()),"max":finite(group.wind_speed_10m_kmh.max()),"mean":finite(group.wind_speed_10m_kmh.mean())}})
 pending=sorted(target for target in set(targets) if actual_by_timestamp.get(target) is None)
 observed=[] if observed_weather_root is None else _observed_weather(observed_weather_root,targets)
 payload={"schema_version":"1.0","status":"prospective_forecast_retrospective_evaluation","forecast_generation_type":"prospective_live_inference","not_current_live_forecast":True,"unit":"MW","issue_id":issue,"forecast_origin_utc":_iso(origin),"context_end_utc":_iso(manifest["context_end_utc"]),"demand_availability_lag_hours":int(manifest["demand_availability_lag_hours"]),"actual_coverage":{"available_target_hours":24-len(pending),"total_target_hours":24,"pending_target_hours":pending,"source":"EIA ERCO/D hourly megawatthours divided by the one-hour interval = average MW."},"models":models,"weather":{"status":"issued_forecast_available_observed_weather_available" if observed else "issued_forecast_available_observed_weather_pending","source":"Open-Meteo issued forecast capture","weather_model":str(manifest["weather_model"]),"weather_model_run_utc":_iso(weather.weather_model_run_utc.iloc[0]),"weather_available_at_utc":_iso(weather.weather_available_at_utc.iloc[0]),"city_hour_count":int(len(weather)),"cities":summaries,"observed_weather_available":bool(observed),"observed_weather_city_hour_count":len(observed),"observed_weather_records":observed,"note":"Issued five-city forecast inputs are summarized here. Observed weather is included only for target city-hours committed to canonical Gold."},"provenance":{"forecast_sha256":manifest["forecast_sha256"],"input_manifest_sha256":manifest["input_manifest_sha256"],"source_artifacts":["issue_manifest.json","forecast_predictions.parquet","weather_forecasts.parquet","Gold v2 EIA actual-demand snapshot","Gold v2 observed-weather snapshot"],"dashboard_published_at_utc":datetime.now(timezone.utc).isoformat()}}
 payload["execution"]={"model_execution_completed_at_utc":None if manifest.get("model_execution_completed_at_utc") is None else _iso(manifest["model_execution_completed_at_utc"]),"dashboard_published_at_utc":payload["provenance"]["dashboard_published_at_utc"]}
 payload["actual_coverage"]["latest_available_target_utc"]=max((row["target_timestamp_utc"] for model in models for row in model["predictions"] if row["actual_demand_mw"] is not None),default=None)
 payload["weather"]["latest_observed_target_utc"]=max((row["target_timestamp_utc"] for row in observed),default=None)
 atomic(output_dir/"prospective-forecast.json",public(validate_document("prospective-forecast.json",payload)))
 return payload

def publish_rolling_historical_replay(evaluation_path,replay_inputs_root,observed_weather_root,output_dir):
 """Publish a bounded, explicitly retrospective 14-day comparison."""
 frame=pd.read_parquet(evaluation_path).copy()
 _require(frame,["issue_id","horizon_hours","target_timestamp_utc","model_id","predicted_demand_mw","actual_demand_mw"],"validated historical replay")
 if len(frame)!=1008 or frame.issue_id.nunique()!=14 or set(frame.model_id)!={"weather_aware_xgboost_curve","weather_aware_bilstm_curve","amazon/chronos-2"}:raise PublishError("Replay cohort is not the verified 14 issue / three model set.")
 frame["target_timestamp_utc"]=pd.to_datetime(frame.target_timestamp_utc,utc=True);frame["issue_timestamp_utc"]=pd.to_datetime(frame.issue_id.str.slice(0,8)+"T060000Z",utc=True)
 if frame.duplicated(["issue_id","horizon_hours","model_id"]).any():raise PublishError("Replay contains duplicate model issue horizons.")
 actual=frame[["issue_id","horizon_hours","target_timestamp_utc","actual_demand_mw"]].drop_duplicates()
 if actual.duplicated(["issue_id","horizon_hours"]).any():raise PublishError("Replay actuals conflict by issue horizon.")
 wide=frame.pivot(index=["issue_id","horizon_hours","target_timestamp_utc"],columns="model_id",values="predicted_demand_mw").reset_index().merge(actual,on=["issue_id","horizon_hours","target_timestamp_utc"],validate="one_to_one").sort_values(["issue_id","horizon_hours"])
 rename={"weather_aware_xgboost_curve":"xgboost_mw","weather_aware_bilstm_curve":"bilstm_mw","amazon/chronos-2":"chronos2_mw"};wide=wide.rename(columns=rename)
 curves=[{"issue_timestamp_utc":_iso(pd.to_datetime(row.issue_id[:8]+"T060000Z",utc=True)),"horizon_hours":int(row.horizon_hours),"target_timestamp_utc":_iso(row.target_timestamp_utc),"actual_demand_mw":finite_or_pending(row.actual_demand_mw),"xgboost_mw":finite(row.xgboost_mw),"bilstm_mw":finite(row.bilstm_mw),"chronos2_mw":finite(row.chronos2_mw)} for row in wide.itertuples()]
 metrics={}
 for days in (7,14):
  cutoff=wide.issue_id.min() if days==14 else sorted(wide.issue_id.unique())[-days]
  subset=frame[frame.issue_id>=cutoff].dropna(subset=["actual_demand_mw"])
  span={}
  for model,group in subset.groupby("model_id",sort=True):
   rows=[{"actual_demand_mw":finite(row.actual_demand_mw),"predicted_demand_mw":finite(row.predicted_demand_mw)} for row in group.itertuples()]
   span[MODEL_LABELS[model]]=_metrics(rows)
  metrics[f"{days}_day"]=span
 issued=[]
 valid_issues=set(frame.issue_id.astype(str))
 for path in sorted(replay_inputs_root.glob("issue=*/weather_forecasts.parquet")):
  issue=path.parent.name.split("=",1)[1];weather=pd.read_parquet(path);_require(weather,["city","valid_time_utc","weather_model_run_utc","weather_available_at_utc","temperature_2m_c"],"issued replay weather")
  if issue not in valid_issues:continue
  weather["valid_time_utc"]=pd.to_datetime(weather.valid_time_utc,utc=True)
  weather=weather[(weather.valid_time_utc>pd.to_datetime(issue[:8]+"T060000Z",utc=True))&(weather.valid_time_utc<=pd.to_datetime(issue[:8]+"T060000Z",utc=True)+pd.Timedelta(hours=24))]
  if len(weather)!=120:raise PublishError(f"{issue} issued weather is not five-city 24-hour coverage.")
  issued.extend({"issue_id":issue,"city":str(row.city).replace("_"," ").title(),"target_timestamp_utc":_iso(row.valid_time_utc),"issued_temperature_2m_c":finite(row.temperature_2m_c),"weather_model_run_utc":_iso(row.weather_model_run_utc),"weather_available_at_utc":_iso(row.weather_available_at_utc)} for row in weather.itertuples())
 observed={}
 if observed_weather_root is not None:
  for row in _observed_weather(observed_weather_root,[x["target_timestamp_utc"] for x in issued]):observed[(row["city"],row["target_timestamp_utc"])]=row
 weather=[dict(row,observed_temperature_2m_c=None if (row["city"],row["target_timestamp_utc"]) not in observed else observed[(row["city"],row["target_timestamp_utc"])]["temperature_2m_c"],observed_at_utc=None if (row["city"],row["target_timestamp_utc"]) not in observed else observed[(row["city"],row["target_timestamp_utc"])]["observed_at_utc"]) for row in issued]
 payload={"schema_version":"1.0","status":"historical_rolling_backtest","historical_not_live":True,"unit":"MW","issue_count":14,"issue_start_utc":_iso(wide.issue_id.min()[:8]+"T060000Z"),"issue_end_utc":_iso(wide.issue_id.max()[:8]+"T060000Z"),"curves":curves,"metrics":metrics,"weather":{"status":"issued_forecasts_with_available_observations","cities":["Austin","Dallas","El Paso","Houston","San Antonio"],"records":weather,"note":"Historical backtest inputs use archived ECMWF IFS issued forecasts. Observed temperature appears only when committed to Gold; null is pending/unavailable, never substituted."},"provenance":{"evaluation_artifact":"validated immutable SageMaker outputs joined to active Gold v2 demand","published_at_utc":datetime.now(timezone.utc).isoformat()}}
 atomic(output_dir/"rolling-historical-replay.json",public(validate_document("rolling-historical-replay.json",payload)))
 return payload

def main():
 p=argparse.ArgumentParser();p.add_argument("--closeout-root",type=Path,required=True);p.add_argument("--output-dir",type=Path,default=Path("public/data"));p.add_argument("--prospective-root",type=Path);p.add_argument("--prospective-issue");p.add_argument("--actuals-parquet",type=Path);p.add_argument("--gold-demand-delta-root",type=Path);p.add_argument("--gold-weather-delta-root",type=Path);a=p.parse_args()
 root=a.closeout_root; demo=pd.read_parquet(root/"historical_demo/heldout_2025-07-01_06z_24h.parquet").sort_values("target_timestamp_utc")
 if len(demo)!=24 or demo.target_timestamp_utc.duplicated().any():raise PublishError("Demo must contain exactly 24 unique rows.")
 ts=pd.to_datetime(demo.target_timestamp_utc,utc=True)
 if list(ts.diff().iloc[1:]) != [pd.Timedelta(hours=1)]*23:raise PublishError("Demo timestamps are not consecutive hourly UTC.")
 origin=pd.to_datetime(demo.forecast_origin_utc.iloc[0],utc=True)
 predictions=[]
 for i,r in enumerate(demo.itertuples(),1):
  if pd.to_datetime(r.target_timestamp_utc,utc=True)!=origin+pd.Timedelta(hours=i):raise PublishError("Horizon timestamp mismatch.")
  predictions.append({"horizon_hours":i,"target_timestamp_utc":pd.to_datetime(r.target_timestamp_utc,utc=True).isoformat(),"actual_demand_mw":finite(r.actual_demand_mw),"xgboost_weather_aware_mw":finite(r.xgboost_weather_aware_predicted_demand_mw),"bilstm_weather_aware_mw":finite(r.bilstm_weather_aware_predicted_demand_mw),"chronos2_mw":finite(r.chronos2_predicted_demand_mw)})
 forecast={"schema_version":"1.0","status":"historical_replay","historical_not_live":True,"forecast_origin_utc":origin.isoformat(),"context_end_utc":(origin-pd.Timedelta(hours=24)).isoformat(),"unit":"MW","model_ids":["xgboost_curve_weather_aware","bilstm_curve_weather_aware","amazon/chronos-2"],"weather_provenance":{"source":"ECMWF IFS archived issued forecast","model_run_utc":pd.to_datetime(demo.weather_model_run_utc.iloc[0],utc=True).isoformat(),"available_at_utc":pd.to_datetime(demo.weather_available_at_utc.iloc[0],utc=True).isoformat()},"predictions":predictions}
 board=json.loads((root/"comparison/leaderboard.json").read_text());per=json.loads((root/"comparison/per_horizon_leaderboard.json").read_text());cat=json.loads((root/"model_catalog.json").read_text())
 performance={"schema_version":"1.0","status":"historical_evaluation","cohort":"strict_common_origin","leaderboard":board,"per_horizon":per,"selected_local_default":cat["selection"]["selected_default"]}
 # The demonstration artifact retains target actuals for backtest comparison,
 # not the demand observations available at issuance.  Keep that distinction
 # explicit instead of presenting hindsight actuals as forecast context.
 demand={"schema_version":"1.0","status":"historical_replay_actuals","historical_not_live":True,"unit":"MW","records":[{"timestamp_utc":x["target_timestamp_utc"],"actual_demand_mw":x["actual_demand_mw"]} for x in predictions],"note":"Historical target actuals for replay evaluation; not demand observations available at forecast issuance."}
 # The final demonstration preserves issued-weather provenance but not the
 # individual city-weather values.  Publish no invented weather outlook.
 weather={"schema_version":"1.0","status":"historical_issued_weather_metadata_only","historical_not_live":True,"forecast_origin_utc":origin.isoformat(),"weather_model_run_utc":forecast["weather_provenance"]["model_run_utc"],"cities":["Austin","Dallas","El Paso","Houston","San Antonio"],"weather_values_available":False,"note":"Five-city issued-weather inputs were used. This artifact retains provenance only; no weather values are fabricated for the public replay."}
 metadata={"schema_version":"1.0","generated_at_utc":datetime.now(timezone.utc).isoformat(),"data_currency":"historical replay only","historical_not_live":True,"source_artifacts":["historical_demo/heldout_2025-07-01_06z_24h.parquet","comparison/leaderboard.json","comparison/per_horizon_leaderboard.json","model_catalog.json"],"forecast_origin_utc":origin.isoformat(),"horizon_hours":24}
 for name,obj in {"latest-forecast.json":forecast,"model-performance.json":performance,"demand-history.json":demand,"weather-summary.json":weather,"forecast-metadata.json":metadata}.items():atomic(a.output_dir/name,public(validate_document(name,obj)))
 optional=[a.prospective_root,a.prospective_issue]
 if any(optional) and not all(optional):p.error("--prospective-root and --prospective-issue must be supplied together")
 if a.actuals_parquet and a.gold_demand_delta_root:p.error("Use either --actuals-parquet or --gold-demand-delta-root, not both")
 if all(optional) and not (a.actuals_parquet or a.gold_demand_delta_root):p.error("Prospective publication requires actuals from --actuals-parquet or --gold-demand-delta-root")
 if a.gold_weather_delta_root and not all(optional):p.error("--gold-weather-delta-root requires prospective publication")
 if a.gold_demand_delta_root:
  actuals_path=a.output_dir/".gold-actuals.parquet";actuals_path.parent.mkdir(parents=True,exist_ok=True);_gold_actuals(a.gold_demand_delta_root).to_parquet(actuals_path,index=False)
 else:actuals_path=a.actuals_parquet
 prospective=publish_prospective(a.prospective_root,actuals_path,a.prospective_issue,a.output_dir,a.gold_weather_delta_root) if all(optional) else None
 print(json.dumps({"files":sorted(x.name for x in a.output_dir.glob("*.json")),"prediction_count":len(predictions),"origin":origin.isoformat(),"prospective_issue":prospective["issue_id"] if prospective else None}))
if __name__=="__main__":main()
