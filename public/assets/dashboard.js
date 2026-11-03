const DATA_FILES = [
  "latest-forecast.json",
  "model-performance.json",
  "demand-history.json",
  "weather-summary.json",
  "forecast-metadata.json",
];
const OPTIONAL_DATA_FILES = ["prospective-forecast.json", "rolling-historical-replay.json", "rolling-prospective-evaluation.json"];
const MODEL_FIELDS = {
  xgboost_curve_weather_aware: "xgboost_weather_aware_mw",
  bilstm_curve_weather_aware: "bilstm_weather_aware_mw",
  "amazon/chronos-2": "chronos2_mw",
};
const MODEL_NAMES = {
  xgboost_curve_weather_aware: "Weather-aware XGBoost",
  bilstm_curve_weather_aware: "Weather-aware Bi-LSTM",
  xgboost_curve_demand_only: "Demand-only XGBoost",
  bilstm_curve_demand_only: "Demand-only Bi-LSTM",
  weather_aware_xgboost_curve: "Weather-aware XGBoost",
  weather_aware_bilstm_curve: "Weather-aware Bi-LSTM",
  "amazon/chronos-2": "Chronos-2 zero-shot",
  seasonal_naive_context_end: "Seasonal-naive baseline",
};
let state = { activeModel: "bilstm_curve_weather_aware", activeProspectiveModel: null, view: "prospective" };
const $ = (id) => document.getElementById(id);
const fmtMW = (v) => `${Math.round(v).toLocaleString("en-US")} MW`;
const fmtGW = (v) => `${(v / 1000).toFixed(1)} GW`;
const fmtMetric = (v) => `${Math.round(v).toLocaleString("en-US")} MW`;
const fmtPct = (v) => `${Number(v).toFixed(2)}%`;
const fmtMaybe = (v, formatter = fmtMetric) => Number.isFinite(v) ? formatter(v) : "—";
const utc = (value) => new Date(value).toLocaleString("en-US", { timeZone: "UTC", timeZoneName: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const central = (value) => new Date(value).toLocaleString("en-US", { timeZone: "America/Chicago", timeZoneName: "short", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
const normalizeIssueId = (value) => {
  const raw = String(value || "").trim();
  if (/^\d{8}T\d{6}Z$/.test(raw)) return raw;
  const parsed = new Date(raw);
  return Number.isNaN(parsed.valueOf()) ? "" : parsed.toISOString().replace(/[-:]/g, "").replace(".000", "");
};

async function loadPublishedData() {
  const entries = await Promise.all(DATA_FILES.map(async (name) => {
    const response = await fetch(`./data/${name}`, { cache: "no-store" });
    if (!response.ok) throw new Error(`Published file unavailable: ${name}`);
    return [name, await response.json()];
  }));
  const optional = await Promise.all(OPTIONAL_DATA_FILES.map(async (name) => {
    const response = await fetch(`./data/${name}`, { cache: "no-store" });
    if (response.status === 404) return null;
    if (!response.ok) throw new Error(`Published optional file unavailable: ${name}`);
    return [name, await response.json()];
  }));
  return Object.fromEntries([...entries, ...optional.filter(Boolean)]);
}
function assertReplay(data) {
  const forecast = data["latest-forecast.json"];
  if (forecast.status !== "historical_replay" || forecast.historical_not_live !== true || forecast.predictions?.length !== 24) throw new Error("The published forecast is not a valid 24-hour historical replay.");
  forecast.predictions.forEach((row, index) => {
    const expected = new Date(forecast.forecast_origin_utc).getTime() + (index + 1) * 3600_000;
    if (row.horizon_hours !== index + 1 || new Date(row.target_timestamp_utc).getTime() !== expected) throw new Error("Published chart timestamps are not hourly and aligned.");
  });
  return forecast;
}
function selectedModel(data) {
  const performance = data["model-performance.json"];
  const validation = performance.leaderboard
    .filter((row) => row.evaluation_split === "validation" && MODEL_FIELDS[row.model_id] && Number.isFinite(row.mae_mw))
    .sort((left, right) => left.mae_mw - right.mae_mw);
  const validationWinner = validation[0]?.model_id;
  const catalogSelection = performance.selected_local_default;
  // The published selection must agree with validation MAE; never silently
  // substitute a more sophisticated model such as Chronos-2.
  if (catalogSelection === validationWinner) return catalogSelection;
  return validationWinner || Object.keys(MODEL_FIELDS)[0];
}
function setSummary(forecast) {
  const rows = forecast.predictions;
  const key = MODEL_FIELDS[state.activeModel];
  const values = rows.map((row) => row[key]);
  const peak = Math.max(...values); const index = values.indexOf(peak);
  $("model-name").textContent = MODEL_NAMES[state.activeModel] || state.activeModel;
  $("model-detail").textContent = "Lowest published validation MAE";
  $("peak-demand").textContent = fmtMW(peak); $("peak-demand-gw").textContent = fmtGW(peak);
  $("peak-hour").textContent = utc(rows[index].target_timestamp_utc); $("peak-hour-local").textContent = `Texas local: ${central(rows[index].target_timestamp_utc)}`;
  $("average-demand").textContent = fmtMW(values.reduce((sum, value) => sum + value, 0) / values.length);
  $("forecast-subtitle").textContent = `Issued ${utc(forecast.forecast_origin_utc)} · targets ${utc(rows[0].target_timestamp_utc)}–${utc(rows.at(-1).target_timestamp_utc)}.`;
}
function temperatureScale(values) {
  const low = Math.min(...values), high = Math.max(...values);
  const paddedLow = low - Math.max(1, (high - low) * .08), paddedHigh = high + Math.max(1, (high - low) * .08);
  const target = Math.max((paddedHigh - paddedLow) / 4, 1);
  const exponent = 10 ** Math.floor(Math.log10(target));
  const step = [1, 2, 2.5, 5, 10].map((factor) => factor * exponent).find((candidate) => candidate >= target) || 10 * exponent;
  const min = Math.floor(paddedLow / step) * step, max = Math.ceil(paddedHigh / step) * step;
  const ticks = [];
  for (let value = min; value <= max + step / 100; value += step) ticks.push(Number(value.toFixed(4)));
  return { min, max, ticks };
}
function utcDateTickIndices(labels, plotWidth) {
  const daily = [], seen = new Set();
  labels.forEach((label, index) => {
    const date = new Date(label).toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
    if (!seen.has(date)) { seen.add(date); daily.push(index); }
  });
  const maximum = Math.max(2, Math.floor(plotWidth / 76));
  const stride = Math.max(1, Math.ceil(daily.length / maximum));
  return daily.filter((_, index) => index % stride === 0 || index === daily.length - 1);
}
function drawChart(canvas, series, labels, tooltip, includeActual = true, options = {}) {
  const rect = canvas.getBoundingClientRect(); const dpr = window.devicePixelRatio || 1;
  canvas.width = Math.max(1, Math.floor(rect.width * dpr)); canvas.height = Math.max(1, Math.floor(rect.height * dpr));
  const ctx = canvas.getContext("2d"); ctx.scale(dpr, dpr); const width = rect.width; const height = rect.height;
  const pad = { left: 55, right: 16, top: 18, bottom: 34 }; const plotW = width - pad.left - pad.right; const plotH = height - pad.top - pad.bottom;
  const all = series.flatMap((s) => s.values).filter(Number.isFinite);
  const temperature = options.axis === "temperature_c";
  const derived = temperature ? temperatureScale(all) : null;
  const spread = Math.max(...all) - Math.min(...all); const margin = options.margin ?? Math.max(spread * .12, 1);
  const min = derived?.min ?? Math.min(...all) - margin, max = derived?.max ?? Math.max(...all) + margin;
  const tickValues = derived?.ticks ?? Array.from({ length: 5 }, (_, index) => min + ((max - min) * index) / 4);
  const x = (i) => pad.left + (i / (labels.length - 1)) * plotW; const y = (v) => pad.top + (1 - (v - min) / (max - min || 1)) * plotH;
  ctx.clearRect(0, 0, width, height); ctx.font = "11px system-ui"; ctx.fillStyle = options.axisColor || (temperature ? "#a9b9cf" : "#5b687b"); ctx.strokeStyle = options.gridColor || (temperature ? "#60738c" : "#e0e7f0");
  tickValues.forEach((value) => { const py = y(value); ctx.beginPath(); ctx.moveTo(pad.left, py); ctx.lineTo(width - pad.right, py); ctx.stroke(); ctx.fillText(options.yLabel ? options.yLabel(value) : `${Math.round(value / 1000)}k`, 5, py + 4); });
  if (options.xTickStyle === "utcDate") {
    ctx.textAlign = "center";
    utcDateTickIndices(labels, plotW).forEach((index) => ctx.fillText(new Date(labels[index]).toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric" }), x(index), height - 11));
    ctx.textAlign = "start";
  } else labels.forEach((label, i) => { if (i % 4 === 0 || i === labels.length - 1) ctx.fillText(new Date(label).toLocaleTimeString("en-US", { timeZone: "UTC", hour: "2-digit", hour12: false }) + "Z", x(i) - 11, height - 11); });
  series.forEach((line) => { ctx.strokeStyle = line.color; ctx.lineWidth = line.width || 2.4; ctx.lineCap = line.lineCap || (temperature ? "round" : "butt"); ctx.lineJoin = temperature ? "round" : "miter"; ctx.setLineDash(line.dash || []); ctx.beginPath(); let connected = false; line.values.forEach((value, i) => { if (!Number.isFinite(value)) { connected = false; return; } if (!connected) ctx.moveTo(x(i), y(value)); else ctx.lineTo(x(i), y(value)); connected = true; }); ctx.stroke(); ctx.setLineDash([]); });
  if (!tooltip) return;
  canvas.onmousemove = (event) => { const i = Math.max(0, Math.min(labels.length - 1, Math.round(((event.offsetX - pad.left) / plotW) * (labels.length - 1)))); const format = options.valueLabel || fmtMW; const lines = series.map((line) => `<div><strong>${line.label}:</strong> ${Number.isFinite(line.values[i]) ? format(line.values[i]) : "Pending"}</div>`).join(""); tooltip.innerHTML = `<strong>${utc(labels[i])}</strong>${lines}`; tooltip.style.left = `${Math.min(event.offsetX + 12, width - 225)}px`; tooltip.style.top = `${Math.max(event.offsetY - 76, 4)}px`; tooltip.classList.remove("hidden"); };
  canvas.onmouseleave = () => tooltip.classList.add("hidden");
}
function renderForecast(forecast) {
  const controls = $("model-toggle"); controls.innerHTML = "";
  forecast.model_ids.filter((id) => MODEL_FIELDS[id]).forEach((id) => { const button = document.createElement("button"); button.type = "button"; button.textContent = MODEL_NAMES[id] || id; button.setAttribute("aria-pressed", String(id === state.activeModel)); button.addEventListener("click", () => { state.activeModel = id; renderForecast(forecast); }); controls.append(button); });
  setSummary(forecast);
  const key = MODEL_FIELDS[state.activeModel];
  drawChart($("forecast-chart"), [{ label: "Actual", values: forecast.predictions.map((r) => r.actual_demand_mw), color: "#dbeafe", width: 2.8 }, { label: MODEL_NAMES[state.activeModel], values: forecast.predictions.map((r) => r[key]), color: "#087e8b", width: 2.8 }], forecast.predictions.map((r) => r.target_timestamp_utc), $("chart-tooltip"));
}
function renderPerformance(performance) {
  const render = () => { const split = $("split-select").value; const rows = performance.leaderboard.filter((row) => row.evaluation_split === split); $("performance-body").innerHTML = rows.map((row) => `<tr><td>${MODEL_NAMES[row.model_id] || row.model_id}</td><td>${fmtMetric(row.mae_mw)}<br><small>${fmtGW(row.mae_mw)}</small></td><td>${fmtMetric(row.rmse_mw)}</td><td>${fmtPct(row.mape_pct)}</td></tr>`).join("") || "<tr><td colspan=\"4\">No published metrics for this split.</td></tr>"; };
  $("split-select").addEventListener("change", render); render();
}
function renderDemand(demand) { $("demand-note").textContent = demand.note; drawChart($("demand-chart"), [{ label: "Actual demand", values: demand.records.map((r) => r.actual_demand_mw), color: "#2267b5" }], demand.records.map((r) => r.timestamp_utc), null, false); }
function renderWeather(weather) { $("weather-content").innerHTML = `<p><strong>Issued forecast model run:</strong> ${utc(weather.weather_model_run_utc)}</p><p><strong>Forecast origin:</strong> ${utc(weather.forecast_origin_utc)}</p><p><strong>Reference cities:</strong> ${weather.cities.join(", ")}</p><p class="explain">${weather.note}</p>`; }
function legacySetDashboardView(view, hasProspective, hasRolling = false, hasProspectivePerformance = false) {
  state.view = view;
  $("prospective-panel").classList.toggle("hidden", !hasProspective || view !== "prospective");
  $("rolling-backtest").classList.toggle("hidden", !hasRolling || (view !== "rolling" && view !== "prospective-performance"));
  $("historical-experiment").classList.toggle("hidden", view !== "historical");
  $("show-prospective").setAttribute("aria-pressed", String(view === "prospective"));
  $("show-rolling").classList.toggle("hidden", !hasRolling);
  $("show-rolling").setAttribute("aria-pressed", String(view === "rolling"));
  $("show-prospective-performance").classList.toggle("hidden", !hasProspectivePerformance);
  $("show-prospective-performance").setAttribute("aria-pressed", String(view === "prospective-performance"));
  $("show-historical").setAttribute("aria-pressed", String(view === "historical"));
}
function legacyRenderProspective(prospective) {
  if (!prospective || prospective.status !== "prospective_forecast_retrospective_evaluation") return;
  const models = prospective.models.filter((model) => Array.isArray(model.predictions) && model.predictions.length === 24);
  if (!models.length) return;
  $("status-pill").textContent = "Published prospective forecast";
  $("page-title").textContent = "Latest published 24-hour forecast";
  $("replay-disclosure").textContent = `Primary view: issue ${utc(prospective.forecast_origin_utc)}. The July replay remains a separate historical experiment.`;
  const ranked = models.filter((model) => Number.isFinite(model.metrics?.mae_mw)).sort((left, right) => left.metrics.mae_mw - right.metrics.mae_mw);
  if (!models.some((model) => model.model_id === state.activeProspectiveModel)) state.activeProspectiveModel = (ranked[0] || models[0]).model_id;
  const selected = models.find((model) => model.model_id === state.activeProspectiveModel) || models[0];
  const controls = $("prospective-model-toggle"); controls.innerHTML = "";
  models.forEach((model) => { const button = document.createElement("button"); button.type = "button"; button.textContent = `${model.model_label} · ${model.variant}`; button.setAttribute("aria-pressed", String(model.model_id === selected.model_id)); button.addEventListener("click", () => { state.activeProspectiveModel = model.model_id; renderProspective(prospective); }); controls.append(button); });
  const rows = selected.predictions;
  const values = rows.map((row) => row.predicted_demand_mw); const peak = Math.max(...values); const peakIndex = values.indexOf(peak);
  const coverage = prospective.actual_coverage;
  $("prospective-origin").textContent = utc(prospective.forecast_origin_utc);
  $("prospective-target-window").textContent = `Targets ${utc(rows[0].target_timestamp_utc)}–${utc(rows.at(-1).target_timestamp_utc)}`;
  $("prospective-execution").textContent = prospective.execution?.model_execution_completed_at_utc ? utc(prospective.execution.model_execution_completed_at_utc) : "Recorded in model lineage";
  const refreshedAt = prospective.execution?.dashboard_published_at_utc || prospective.provenance?.dashboard_published_at_utc;
  $("prospective-freshness").textContent = refreshedAt ? `Dashboard refreshed ${utc(refreshedAt)}` : "Dashboard publication timestamp unavailable";
  $("prospective-coverage").textContent = `${coverage.available_target_hours}/${coverage.total_target_hours}`;
  $("prospective-pending").textContent = coverage.pending_target_hours.length ? `Pending: ${coverage.pending_target_hours.map(utc).join(", ")}` : "All target actuals available";
  $("prospective-peak").textContent = fmtMW(peak); $("prospective-peak-time").textContent = utc(rows[peakIndex].target_timestamp_utc);
  $("prospective-mae").textContent = fmtMaybe(selected.metrics.mae_mw); $("prospective-variant").textContent = `${selected.model_label} · ${selected.variant} output`;
  $("prospective-disclosure").textContent = `This is the latest genuine prospective issuance from ${utc(prospective.forecast_origin_utc)}. ${coverage.available_target_hours} of ${coverage.total_target_hours} EIA target observations were available when this static export was created; unavailable observations remain pending.`;
  drawChart($("prospective-chart"), [{ label: "Available EIA actual", values: rows.map((row) => row.actual_demand_mw), color: "#dbeafe", width: 2.8 }, { label: `${selected.model_label} (${selected.variant})`, values, color: "#087e8b", width: 2.8 }], rows.map((row) => row.target_timestamp_utc), $("prospective-tooltip"));
  $("prospective-performance-body").innerHTML = models.map((model) => `<tr><td>${model.model_label}</td><td>${model.variant}</td><td>${model.metrics.matched_observation_count}/24</td><td>${fmtMaybe(model.metrics.mae_mw)}</td><td>${fmtMaybe(model.metrics.rmse_mw)}</td><td>${fmtMaybe(model.metrics.bias_mw)}</td></tr>`).join("");
  const weather = prospective.weather;
  $("issued-weather-note").textContent = `${weather.weather_model.toUpperCase()} run ${utc(weather.weather_model_run_utc)}; conservatively available at ${utc(weather.weather_available_at_utc)}. ${weather.city_hour_count} issued city-hour inputs are represented. ${weather.observed_weather_city_hour_count || 0} observed city-hours are currently committed to canonical Gold; missing observations remain pending.`;
  $("issued-weather-content").innerHTML = weather.cities.map((city) => `<article class="weather-card"><h4>${city.city}</h4><p>Temp: ${city.temperature_2m_c.min.toFixed(1)}–${city.temperature_2m_c.max.toFixed(1)} °C</p><p>Humidity: ${city.relative_humidity_pct.min.toFixed(0)}–${city.relative_humidity_pct.max.toFixed(0)}%</p><p>Wind: ${city.wind_speed_10m_kmh.min.toFixed(1)}–${city.wind_speed_10m_kmh.max.toFixed(1)} km/h</p></article>`).join("");
  $("dashboard-view-toggle").classList.remove("hidden");
  $("show-prospective").onclick = () => setDashboardView("prospective", true);
  $("show-historical").onclick = () => setDashboardView("historical", true);
  setDashboardView(state.view, true);
}
function renderRolling(rolling) {
  if (!rolling || rolling.status !== "historical_rolling_backtest" || rolling.historical_not_live !== true) return;
  const issueSelect = $("rolling-issue"), citySelect = $("rolling-city"), windowSelect = $("rolling-window");
  const issues = [...new Set(rolling.curves.map((row) => row.issue_timestamp_utc))].sort();
  issueSelect.innerHTML = issues.map((issue) => `<option value="${issue}">${utc(issue)}</option>`).join("");
  citySelect.innerHTML = rolling.weather.cities.map((city) => `<option value="${city}">${city}</option>`).join("");
  issueSelect.value = issues.at(-1); citySelect.value = rolling.weather.cities[0];
  const render = () => {
    const selectedIssueIndex = issues.indexOf(issueSelect.value);
    const windowIssueCount = windowSelect.value === "7_day" ? 7 : 14;
    const windowIssues = new Set(issues.slice(Math.max(0, selectedIssueIndex - windowIssueCount + 1), selectedIssueIndex + 1));
    const rows = rolling.curves.filter((row) => windowIssues.has(row.issue_timestamp_utc)).sort((a, b) => new Date(a.target_timestamp_utc) - new Date(b.target_timestamp_utc));
    drawChart($("rolling-chart"), [{ label: "EIA actual", values: rows.map((row) => row.actual_demand_mw), color: "#dbeafe", width: 2.8 }, { label: "XGBoost", values: rows.map((row) => row.xgboost_mw), color: "#087e8b" }, { label: "Bi-LSTM", values: rows.map((row) => row.bilstm_mw), color: "#d06b28" }, { label: "Chronos-2", values: rows.map((row) => row.chronos2_mw), color: "#7a4aa8" }], rows.map((row) => row.target_timestamp_utc), $("rolling-tooltip"));
    $("rolling-demand-note").textContent = `Showing ${rows.length} forecast target hours from ${windowIssues.size} issued forecast evaluations ending ${utc(issueSelect.value)}. Missing EIA actuals are shown as gaps, never as zeros. All values are MW and timestamps are UTC.`;
    const metrics = rolling.metrics[windowSelect.value];
    $("rolling-metrics").innerHTML = Object.entries(metrics).map(([model, values]) => `<tr><td>${model}</td><td>${values.matched_observation_count}</td><td>${fmtMaybe(values.mae_mw)}</td><td>${fmtMaybe(values.rmse_mw)}</td><td>${fmtMaybe(values.bias_mw)}</td></tr>`).join("");
    const windowIssueIds = new Set([...windowIssues].map(normalizeIssueId));
    const weather = rolling.weather.records.filter((row) => windowIssueIds.has(normalizeIssueId(row.issue_id)) && row.city === citySelect.value).sort((a, b) => new Date(a.target_timestamp_utc) - new Date(b.target_timestamp_utc));
    drawChart($("rolling-weather-chart"), [{ label: "Issued ECMWF IFS temperature", values: weather.map((row) => row.issued_temperature_2m_c), color: "#31c6c9", width: 2.8, lineCap: "round" }, { label: "Observed Open-Meteo temperature", values: weather.map((row) => row.observed_temperature_2m_c), color: "#f1f5f9", width: 3.2, dash: [8, 5], lineCap: "round" }], weather.map((row) => row.target_timestamp_utc), $("rolling-weather-tooltip"), true, { axis: "temperature_c", xTickStyle: "utcDate", yLabel: (value) => `${value.toFixed(0)} °C`, valueLabel: (value) => `${value.toFixed(1)} °C` });
    const observed = weather.filter((row) => Number.isFinite(row.observed_temperature_2m_c)).length;
    $("rolling-weather-note").textContent = observed ? `${citySelect.value}: ${observed} of ${weather.length} hours observed. The remaining ${weather.length - observed} target hours are intentional gaps because no genuine historical observation is available.` : `${citySelect.value}: observed weather is pending from the Open-Meteo archive. The dashed observed line appears only after a complete valid source capture reaches Gold.`;
  };
  issueSelect.onchange = render; citySelect.onchange = render; windowSelect.onchange = render; render();
  $("show-rolling").onclick = () => { $("rolling-heading").textContent = "Rolling issued-forecast evaluation"; setDashboardView("rolling", Boolean($('prospective-panel')), true); };
  $("show-historical").onclick = () => setDashboardView("historical", Boolean($('prospective-panel')), true);
  $("show-prospective").onclick = () => setDashboardView("prospective", Boolean($('prospective-panel')), true);
}
function legacyRenderProspectiveRolling(rolling) {
  if (!rolling || rolling.status !== "prospective_rolling_evaluation" || rolling.evaluation_scope !== "genuine_prospective_performance") return;
  const issueSelect = $("rolling-issue"), citySelect = $("rolling-city"), windowSelect = $("rolling-window");
  const issues = rolling.issues || [];
  if (!issues.length) return;
  const render = () => {
    const issue = issueSelect.value, curves = rolling.curves[issue];
    if (!curves) return;
    const byModel = Object.fromEntries(Object.entries(curves).map(([model, rows]) => [model, Object.fromEntries(rows.map((row) => [row.horizon_hours, row]))]));
    const rows = Object.keys(byModel.xgboost || {}).map(Number).sort((a, b) => a - b).map((horizon) => ({ horizon_hours: horizon, target_timestamp_utc: byModel.xgboost[horizon].target_timestamp_utc, actual_demand_mw: byModel.xgboost[horizon].actual_demand_mw, xgboost_mw: byModel.xgboost[horizon].predicted_demand_mw, bilstm_mw: byModel.bilstm?.[horizon]?.predicted_demand_mw, chronos2_mw: byModel.chronos2?.[horizon]?.predicted_demand_mw }));
    drawChart($("rolling-chart"), [{ label: "EIA actual", values: rows.map((row) => row.actual_demand_mw), color: "#dbeafe", width: 2.8 }, { label: "XGBoost", values: rows.map((row) => row.xgboost_mw), color: "#087e8b" }, { label: "Bi-LSTM", values: rows.map((row) => row.bilstm_mw), color: "#d06b28" }, { label: "Chronos-2", values: rows.map((row) => row.chronos2_mw), color: "#7a4aa8" }], rows.map((row) => row.target_timestamp_utc), $("rolling-tooltip"));
    const metrics = rolling.metrics[windowSelect.value] || {};
    $("rolling-metrics").innerHTML = Object.entries(metrics).map(([model, values]) => `<tr><td>${model}</td><td>${values.matched_observation_count}</td><td>${fmtMaybe(values.mae_mw)}</td><td>${fmtMaybe(values.rmse_mw)}</td><td>${fmtMaybe(values.bias_mw)}</td></tr>`).join("");
    const weather = (rolling.weather?.records || []).filter((row) => row.issue_id === issue && row.city === citySelect.value).sort((a, b) => new Date(a.target_timestamp_utc) - new Date(b.target_timestamp_utc));
    drawChart($("rolling-weather-chart"), [{ label: "Issued IFS temperature", values: weather.map((row) => row.issued_temperature_2m_c), color: "#087e8b" }, { label: "Gold observed temperature", values: weather.map((row) => row.observed_temperature_2m_c), color: "#14233a", dash: [5, 3] }], weather.map((row) => row.target_timestamp_utc), $("rolling-weather-tooltip"));
    const observed = weather.filter((row) => Number.isFinite(row.observed_temperature_2m_c)).length;
    $("rolling-weather-note").textContent = `${citySelect.value}: ${observed}/${weather.length} target hours have genuine Gold observed temperature. Missing observations remain pending; issued weather is the original ECMWF IFS input.`;
  };
  issueSelect.innerHTML = issues.map((issue) => `<option value="${issue}">${utc(`${issue.slice(0, 8)}T${issue.slice(9, 15)}Z`)}</option>`).join("");
  const cities = [...new Set((rolling.weather?.records || []).map((row) => row.city))].sort();
  citySelect.innerHTML = cities.map((city) => `<option value="${city}">${city}</option>`).join("");
  issueSelect.value = issues.at(-1); citySelect.value = cities[0] || "";
  issueSelect.onchange = render; citySelect.onchange = render; windowSelect.onchange = render;
  $("show-prospective-performance").onclick = () => { $("rolling-heading").textContent = "Rolling genuine prospective performance"; setDashboardView("prospective-performance", true, Boolean($("show-rolling") && !$("show-rolling").classList.contains("hidden")), true); render(); };
}
function renderProvenance(meta, forecast, prospective) { $("publication-time").textContent = prospective?.execution?.dashboard_published_at_utc ? `Data refreshed ${utc(prospective.execution.dashboard_published_at_utc)}` : `Published ${utc(meta.generated_at_utc)}`; if (!prospective) $("replay-disclosure").textContent = `This is a saved historical replay issued ${utc(forecast.forecast_origin_utc)}. It is not a live 2026 forecast and cannot trigger model inference.`; const entries = prospective ? [["Primary publication", `Prospective issue ${prospective.issue_id}`], ["Issue time", utc(prospective.forecast_origin_utc)], ["Model execution", prospective.execution?.model_execution_completed_at_utc ? utc(prospective.execution.model_execution_completed_at_utc) : "Available in immutable lineage"], ["Evaluation freshness", prospective.actual_coverage?.latest_available_target_utc ? `EIA through ${utc(prospective.actual_coverage.latest_available_target_utc)}` : "No target actuals committed yet"], ["Source artifacts", prospective.provenance.source_artifacts.join(", ")]] : [["Publication status", "Historical replay — not live"], ["Forecast origin", utc(forecast.forecast_origin_utc)], ["Target period", `${utc(forecast.predictions[0].target_timestamp_utc)} to ${utc(forecast.predictions.at(-1).target_timestamp_utc)}`], ["Model inputs", "Published static JSON only"], ["Source artifacts", meta.source_artifacts.join(", ")]]; $("provenance-list").innerHTML = entries.map(([key, value]) => `<dt>${key}</dt><dd>${value}</dd>`).join(""); $("provenance-button").addEventListener("click", () => { const panel = $("provenance-panel"); const hidden = panel.classList.toggle("hidden"); $("provenance-button").setAttribute("aria-expanded", String(!hidden)); }); }
async function legacyMain() { try { const data = await loadPublishedData(); const forecast = assertReplay(data); const prospective = data["prospective-forecast.json"], rolling = data["rolling-historical-replay.json"], prospectiveRolling = data["rolling-prospective-evaluation.json"]; state.activeModel = selectedModel(data); legacyRenderProspective(prospective); renderRolling(rolling); legacyRenderProspectiveRolling(prospectiveRolling); renderForecast(forecast); renderPerformance(data["model-performance.json"]); renderDemand(data["demand-history.json"]); renderWeather(data["weather-summary.json"]); renderProvenance(data["forecast-metadata.json"], forecast, prospective); if (!prospective) setDashboardView("historical", false, Boolean(rolling), Boolean(prospectiveRolling)); else setDashboardView("prospective", true, Boolean(rolling), Boolean(prospectiveRolling)); $("loading-state").classList.add("hidden"); $("dashboard").classList.remove("hidden"); } catch (error) { $("loading-state").classList.add("hidden"); $("error-state").textContent = `Dashboard data could not be loaded safely. ${error.message}`; $("error-state").classList.remove("hidden"); } }
function setDashboardView(view, hasProspective = false, hasRolling = false, hasProspectivePerformance = false) {
  state.view = view;
  $("prospective-panel").classList.toggle("hidden", !hasProspective || view !== "prospective");
  $("prospective-rolling").classList.toggle("hidden", !hasProspectivePerformance || view !== "prospective-performance");
  $("rolling-backtest").classList.toggle("hidden", !hasRolling || view !== "rolling");
  $("historical-experiment").classList.toggle("hidden", view !== "historical");
  [["show-prospective", "prospective"], ["show-prospective-performance", "prospective-performance"], ["show-rolling", "rolling"], ["show-historical", "historical"]].forEach(([id, selected]) => $(id).setAttribute("aria-pressed", String(view === selected)));
  $("show-rolling").classList.toggle("hidden", !hasRolling);
  $("show-prospective-performance").classList.toggle("hidden", !hasProspectivePerformance);
}
function renderProspective(prospective) {
  if (!prospective || prospective.status !== "prospective_forecast_retrospective_evaluation") return;
  const models = prospective.models.filter((model) => Array.isArray(model.predictions) && model.predictions.length === 24);
  if (models.length !== 3) return;
  const rows = models[0].predictions, coverage = prospective.actual_coverage || {}, labels = rows.map((row) => row.target_timestamp_utc);
  const issueDate = new Date(prospective.forecast_origin_utc).toLocaleDateString("en-US", { timeZone: "UTC", month: "short", day: "numeric" });
  $("show-prospective").textContent = `Latest forecast · ${issueDate}`;
  $("status-pill").textContent = "Verified prospective forecast";
  $("page-title").textContent = "ERCOT demand forecast";
  $("replay-disclosure").textContent = `Issued ${utc(prospective.forecast_origin_utc)}. Three independent model curves, live data contract, and evidence-backed evaluation.`;
  $("prospective-origin").textContent = utc(prospective.forecast_origin_utc);
  $("prospective-target-window").textContent = `${utc(labels[0])} to ${utc(labels.at(-1))}`;
  $("prospective-target-short").textContent = `${labels.length} hours`;
  $("prospective-execution").textContent = prospective.execution?.model_execution_completed_at_utc ? utc(prospective.execution.model_execution_completed_at_utc) : "Recorded in lineage";
  const refreshedAt = prospective.execution?.dashboard_published_at_utc || prospective.provenance?.dashboard_published_at_utc;
  $("prospective-freshness").textContent = refreshedAt ? `Published ${utc(refreshedAt)}` : "Publication time unavailable";
  const availableActuals = coverage.available_target_hours || 0, totalActuals = coverage.total_target_hours || labels.length;
  $("prospective-coverage").textContent = `${Math.round((availableActuals / totalActuals) * 100)}%`;
  $("prospective-pending").textContent = availableActuals ? `${availableActuals} verified EIA target hours` : "No EIA target actuals published yet";
  const colors = ["#087e8b", "#e06d2f", "#7555b5"];
  drawChart($("prospective-chart"), [{ label: "EIA actual", values: rows.map((row) => row.actual_demand_mw), color: "#dbeafe", width: 2.8 }, ...models.map((model, index) => ({ label: model.model_label, values: model.predictions.map((row) => row.predicted_demand_mw), color: colors[index], width: 2.5 }))], labels, $("prospective-tooltip"));
  $("forecast-model-peaks").innerHTML = models.map((model) => { const peak = model.predictions.reduce((best, row) => row.predicted_demand_mw > best.predicted_demand_mw ? row : best); return `<article><p>${model.model_label}</p><strong>${fmtMW(peak.predicted_demand_mw)}</strong><span>${utc(peak.target_timestamp_utc)}</span></article>`; }).join("");
  $("prospective-performance-body").innerHTML = models.map((model) => { const metric = (value) => Number.isFinite(value) ? fmtMetric(value) : "Pending actuals"; return `<tr><td>${model.model_label}</td><td>${model.variant}</td><td>${model.metrics?.matched_observation_count || 0}/24</td><td>${metric(model.metrics?.mae_mw)}</td><td>${metric(model.metrics?.rmse_mw)}</td><td>${metric(model.metrics?.bias_mw)}</td></tr>`; }).join("");
  const weather = prospective.weather;
  $("issued-weather-note").textContent = `${weather.weather_model.toUpperCase()} run ${utc(weather.weather_model_run_utc)}. ${weather.city_hour_count} issued city-hour inputs. ${weather.observed_weather_city_hour_count || 0} observed city-hours are available; all other observations remain pending.`;
  $("issued-weather-content").innerHTML = weather.cities.map((city) => `<article class="weather-card"><h4>${city.city}</h4><p>Temperature ${city.temperature_2m_c.min.toFixed(1)} to ${city.temperature_2m_c.max.toFixed(1)} C</p><p>Humidity ${city.relative_humidity_pct.min.toFixed(0)} to ${city.relative_humidity_pct.max.toFixed(0)}%</p><p>Wind ${city.wind_speed_10m_kmh.min.toFixed(1)} to ${city.wind_speed_10m_kmh.max.toFixed(1)} km/h</p></article>`).join("");
}
function renderProspectiveRolling(rolling) {
  if (!rolling || rolling.status !== "prospective_rolling_evaluation" || rolling.evaluation_scope !== "genuine_prospective_performance") return;
  const issueSelect = $("prospective-issue"), windowSelect = $("prospective-window"), citySelect = $("prospective-city"), allIssues = rolling.issues || [];
  const issues = allIssues.length > 1 ? allIssues.slice(0, -1) : allIssues;
  const issueUtc = (issue) => `${issue.slice(0, 4)}-${issue.slice(4, 6)}-${issue.slice(6, 8)}T${issue.slice(9, 11)}:${issue.slice(11, 13)}:${issue.slice(13, 15)}Z`;
  issueSelect.innerHTML = issues.map((issue) => `<option value="${issue}">${utc(issueUtc(issue))}</option>`).join("");
  const cities = [...new Set((rolling.weather?.records || []).map((row) => row.city))].sort(); citySelect.innerHTML = cities.map((city) => `<option>${city}</option>`).join("");
  issueSelect.value = issues.at(-1); citySelect.value = cities[0] || "";
  const render = () => {
    const selectedIndex = issues.indexOf(issueSelect.value);
    const requestedIssueCount = Number(windowSelect.value);
    const selectedIssues = issues.slice(Math.max(0, selectedIndex - requestedIssueCount + 1), selectedIndex + 1);
    const names = ["xgboost", "bilstm", "chronos2"].filter((name) =>
      selectedIssues.some((issue) => Array.isArray(rolling.curves?.[issue]?.[name]))
    );
    const rows = selectedIssues.flatMap((issue) => {
      const curves = rolling.curves?.[issue] || {}, first = curves[names[0]] || [];
      return first.map((row, index) => ({
        target: row.target_timestamp_utc,
        actual: row.actual_demand_mw,
        values: names.map((name) => curves[name]?.[index]?.predicted_demand_mw),
      }));
    }).sort((a, b) => new Date(a.target) - new Date(b.target));
    const colors = ["#087e8b", "#e06d2f", "#7555b5"];
    drawChart($("prospective-rolling-chart"), [{ label: "EIA actual", values: rows.map((row) => row.actual), color: "#dbeafe", width: 2.8 }, ...names.map((name, index) => ({ label: name === "xgboost" ? "XGBoost" : name === "bilstm" ? "Bi-LSTM" : "Chronos-2", values: rows.map((row) => row.values[index]), color: colors[index] }))], rows.map((row) => row.target), $("prospective-rolling-tooltip"));
    const metrics = names.map((name, index) => {
      const pairs = rows.map((row) => [row.actual, row.values[index]]).filter(([actual, prediction]) => Number.isFinite(actual) && Number.isFinite(prediction));
      const errors = pairs.map(([actual, prediction]) => prediction - actual), count = errors.length;
      return { name, matched_observation_count: count, mae_mw: count ? errors.reduce((total, error) => total + Math.abs(error), 0) / count : null, rmse_mw: count ? Math.sqrt(errors.reduce((total, error) => total + error ** 2, 0) / count) : null, bias_mw: count ? errors.reduce((total, error) => total + error, 0) / count : null };
    });
    $("prospective-rolling-metrics").innerHTML = metrics.map((value) => `<tr><td>${value.name === "xgboost" ? "XGBoost" : value.name === "bilstm" ? "Bi-LSTM" : "Chronos-2"}</td><td>${value.matched_observation_count}</td><td>${fmtMaybe(value.mae_mw)}</td><td>${fmtMaybe(value.rmse_mw)}</td><td>${fmtMaybe(value.bias_mw)}</td></tr>`).join("");
    $("prospective-rolling-note").textContent = `${selectedIssues.length} completed forecast issues and ${rows.length} target hours ending ${utc(issueUtc(issueSelect.value))}. Missing EIA observations remain pending and do not enter MAE, RMSE, or bias.`;
    const weather = (rolling.weather?.records || []).filter((row) => selectedIssues.includes(row.issue_id) && row.city === citySelect.value).sort((a, b) => new Date(a.target_timestamp_utc) - new Date(b.target_timestamp_utc));
    const observed = weather.filter((row) => Number.isFinite(row.observed_temperature_2m_c)).length;
    drawChart($("prospective-weather-chart"), [{ label: "Issued IFS temperature", values: weather.map((row) => row.issued_temperature_2m_c), color: "#31c6c9" }, { label: "Observed temperature", values: weather.map((row) => row.observed_temperature_2m_c), color: "#f1f5f9", dash: [5, 3] }], weather.map((row) => row.target_timestamp_utc), $("prospective-weather-tooltip"), true, { yLabel: (value) => `${value.toFixed(0)} C`, valueLabel: (value) => `${value.toFixed(1)} C` });
    $("prospective-weather-note").textContent = observed ? `${citySelect.value}: ${observed}/${weather.length} observed hours available. Remaining gaps are pending.` : `${citySelect.value}: observed weather is pending from the Open-Meteo archive. The dashed observed line appears only after a complete valid source capture reaches Gold.`;
  };
  issueSelect.onchange = render; windowSelect.onchange = render; citySelect.onchange = render; render();
}
function renderLastScoredIssue(rolling) {
  const issues = rolling?.issues || []; const scored = issues.map((issue) => ({ issue, rows: rolling.curves?.[issue]?.xgboost || [] })).filter(({ rows }) => rows.some((row) => Number.isFinite(row.actual_demand_mw)));
  if (!scored.length) return;
  const { issue, rows } = scored.at(-1), peak = rows.filter((row) => Number.isFinite(row.actual_demand_mw)).reduce((best, row) => row.actual_demand_mw > best.actual_demand_mw ? row : best);
  const curves = rolling.curves[issue], model = (name) => curves[name]?.find((row) => row.horizon_hours === peak.horizon_hours)?.predicted_demand_mw;
  const cell = (name, label) => { const value = model(name), delta = value - peak.actual_demand_mw, direction = delta > 0 ? "up" : delta < 0 ? "down" : "flat", arrow = delta > 0 ? "↑" : delta < 0 ? "↓" : "•"; return `<td><strong>${fmtMW(value)}</strong><br><span class="delta-${direction}">${arrow} ${fmtMW(Math.abs(delta))} ${delta > 0 ? "high" : delta < 0 ? "low" : "exact"}</span></td>`; };
  $("last-scored-note").textContent = `Issue ${issue} at the observed peak hour ${utc(peak.target_timestamp_utc)}. This is genuine scored evidence, separate from the pending latest issue.`;
  $("last-scored-body").innerHTML = `<tr><td><strong>${fmtMW(peak.actual_demand_mw)}</strong><br><span>${utc(peak.target_timestamp_utc)}</span></td>${cell("xgboost", "XGBoost")}${cell("bilstm", "Bi-LSTM")}${cell("chronos2", "Chronos-2")}</tr>`;
  $("last-scored-issue").classList.remove("hidden");
}
async function main() { try { const data = await loadPublishedData(), forecast = assertReplay(data), prospective = data["prospective-forecast.json"], rolling = data["rolling-historical-replay.json"], prospectiveRolling = data["rolling-prospective-evaluation.json"], afterLayout = () => new Promise(requestAnimationFrame); state.activeModel = selectedModel(data); $("dashboard-view-toggle").classList.remove("hidden"); $("dashboard").classList.remove("hidden"); setDashboardView("prospective", true, Boolean(rolling), Boolean(prospectiveRolling)); await afterLayout(); renderProspective(prospective); renderProspectiveRolling(prospectiveRolling); renderLastScoredIssue(prospectiveRolling); renderRolling(rolling); renderForecast(forecast); renderPerformance(data["model-performance.json"]); renderDemand(data["demand-history.json"]); renderWeather(data["weather-summary.json"]); renderProvenance(data["forecast-metadata.json"], forecast, prospective); $("show-prospective").onclick = () => { setDashboardView("prospective", true, Boolean(rolling), Boolean(prospectiveRolling)); requestAnimationFrame(() => renderProspective(prospective)); }; $("show-prospective-performance").onclick = () => { setDashboardView("prospective-performance", true, Boolean(rolling), Boolean(prospectiveRolling)); requestAnimationFrame(() => renderProspectiveRolling(prospectiveRolling)); }; $("show-rolling").onclick = () => { setDashboardView("rolling", true, Boolean(rolling), Boolean(prospectiveRolling)); requestAnimationFrame(() => renderRolling(rolling)); }; $("show-historical").onclick = () => { setDashboardView("historical", true, Boolean(rolling), Boolean(prospectiveRolling)); requestAnimationFrame(() => { renderForecast(forecast); renderDemand(data["demand-history.json"]); }); }; $("loading-state").classList.add("hidden"); } catch (error) { $("loading-state").classList.add("hidden"); $("error-state").textContent = `Dashboard data could not be loaded safely. ${error.message}`; $("error-state").classList.remove("hidden"); } }
main();
