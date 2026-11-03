# Project structure and local artifact policy

This repository contains reviewed source code, tests, infrastructure definitions, and a deliberately small static dashboard demo.  It does **not** contain the local datasets, trained-model binaries, runtime secrets, or forecast-output history required for local execution.

## Repository contents

- `src/texas_energy/ingestion/` — provider clients, validated capture contracts, Bronze persistence, and resumable backfill orchestration.
- `src/texas_energy/data/` and `src/texas_energy/spark/` — Bronze-to-Silver and Silver-to-Gold transformations and coverage validation.
- `src/texas_energy/forecast/` — curve contracts, point-in-time weather features, model evaluation, catalog, prospective-input validation, and local serving adapters.
- `src/texas_energy/analytics/` — read-only deterministic analytics tools and the private agent adapter.
- `src/texas_energy/retrieval/` — local document indexing and retrieval; indexes are generated outside the repository.
- `scripts/` — explicit local commands.  Scripts that call providers or AWS are opt-in; test commands must remain offline by default.
- `tests/` — portable unit/regression tests.  Spark, GPU, live-provider, and large-artifact tests require their documented local runtime and artifacts.
- `infra/terraform/` — protected infrastructure roots.  Terraform state, plans, variables containing secrets, and `.terraform/` directories must never be committed.
- `public/` — static dashboard assets and the reviewed, historical-demo JSON contract only.
- `docs/` — user-facing architecture, forecasting, ingestion, and local-operation documentation.

## Persistent local runtime layout

The preferred durable local layout is outside the Git checkout:

```text
~/texas-energy/
  data/{historical,weather,gold}/
  models/{xgboost,bilstm,chronos}/
  rag/indexes/
  forecasts/prospective/{inputs,outputs,logs}/
  evaluations/
  experiments/
  archives/
~/.config/texas-energy/
```

Configuration in `~/.config/texas-energy/` is local-only.  A repository `.env` may supply a local EIA key for an explicit command, but is ignored and never logged.  Trained model binaries, Parquet/Delta data, Hugging Face cache contents, MLflow databases, RAG indexes, prospective forecast inputs/outputs, and logs are also local-only.

## Lifecycle policy

| Category | Repository policy | Current examples |
| --- | --- | --- |
| Active runtime dependencies | Preserve exactly; verify before changing | Prospective scheduler wrapper, local runtime config, final closeout artifacts, Chronos snapshot |
| Historical/evaluation artifacts | Preserve outside Git; document provenance | held-out prediction files, comparison reports, MLflow databases |
| Rebuildable caches | Exclude from Git; recreate from reviewed sources | Python caches, `.pytest_cache`, generated RAG indexes |
| Disposable output | Remove only after an explicit inventory and verified replacement | temporary test files, stale logs, old local build output |
| Uncertain material | Do not move or delete | notebooks, unknown experiments, Terraform working material |

## Prospective trial protection window

`TexasEnergy-ProspectiveForecastTrial` has a pending final scheduled occurrence.  Until its completion is verified, do not move, rename, delete, or rewrite any scheduler-referenced wrapper, runtime configuration, final model artifact, Chronos cache, prospective input/output root, or log root.  The prior issue directory remains immutable.

After the scheduled run, a cleanup may proceed only after: (1) saving task action/trigger evidence, (2) hashing the referenced model artifacts and prior forecast outputs, (3) validating a replacement path with the real wrapper in a non-prospective mode, and (4) retaining an explicit rollback map.  Prefer a same-filesystem move followed by a read-only verification; do not duplicate large models merely to reorganize them.

## Commit boundary

Commit reviewed source, tests, static demo data, documentation, and non-secret configuration templates.  Do not commit `.env`, local `config` values that contain credentials, datasets, model binaries, Terraform state/plans, notebooks without explicit review, logs, caches, or generated local indexes.
