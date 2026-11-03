# Local ingestion contracts

Phase 1A defines local, in-memory source contracts only. It produces raw
payload bytes and serializable manifests in memory; it does not persist objects
to S3. It does not call a live API during tests, retrieve secrets, or contain a
Lambda handler. S3 persistence, Lambda execution, scheduling, and controlled
live API contract validation are future milestones.

## Modules

- `texas_energy.ingestion.http` provides a standard-library HTTP transport,
  retry policy, and injectable transport boundary. It retains response bytes
  without logging request URLs, headers, parameters, or bodies.
- `texas_energy.ingestion.eia` implements the ERCOT demand request contract:
  EIA APIv2 `electricity/rto/region-data/data`, `ERCO`, `D`, hourly UTC,
  deterministic ascending pagination, and validation of raw values and units.
- `texas_energy.ingestion.open_meteo` declares the five notebook reference
  cities, historical and forecast endpoints, explicit hourly fields, and UTC
  timestamp validation using `timeformat=unixtime` and `timezone=GMT`.
- `texas_energy.ingestion.raw` creates deterministic, credential-independent
  request fingerprints and in-memory response envelopes. It rejects endpoint
  query strings and fragments and retains only redacted request metadata. A
  later S3 writer can persist `RawCapture.raw_payload` and
  `RawCapture.manifest_bytes()` directly.

## Running local tests

```powershell
python -m pip install .
python -m pytest -q
```

Tests use scripted in-memory transports. They neither open sockets nor require
an EIA API key. The repository ignores `*.json`, so source-response fixtures
are intentionally small Python builders rather than untracked JSON files.

## Contract assumptions awaiting a controlled live verification

- The EIA request fields and response shape come from the checked-in notebook
  implementations. The contract expects `response.data`, a UTC `period` in
  `YYYY-MM-DDTHH`, and `value-units`; it preserves both `value` and
  `value-units` without calling the value MW.
- The implementation supports EIA's documented `response.total` when present,
  but does not require it because existing notebook code paginates by short
  page. A credentialed, manually approved smoke test should verify both
  production response variants before scheduling ingestion.
- Historical Open-Meteo requests intentionally do not pin a `models` value.
  Choosing a stable reanalysis model such as ERA5, and its resulting latency,
  remains a data-governance decision. Forecast responses are snapshots, not
  observations.
- `forecast_hours` is validated as positive rather than bounded to an assumed
  provider maximum. The initial 48-hour operational horizon should be verified
  against the approved Open-Meteo usage tier before deployment.
