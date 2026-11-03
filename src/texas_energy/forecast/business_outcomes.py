"""Honest, machine-readable business-outcome report scaffolding.

It intentionally records unknown outcomes as ``null``.  This avoids turning
unfinished forecast evaluation into fabricated business claims.
"""

from __future__ import annotations

from datetime import datetime, timezone


def pending_business_outcome_report(*, catalog_path: str, evaluation_path: str) -> dict[str, object]:
    """Create a stable report shell filled only after validated evaluation."""

    return {
        "schema_version": "1.0",
        "created_at_utc": datetime.now(timezone.utc).isoformat(),
        "status": "pending_validated_full_archive_evaluation",
        "inputs": {"model_catalog": catalog_path, "evaluation_artifacts": evaluation_path},
        "outcomes": {
            "forecast_accuracy": None,
            "baseline_comparison": None,
            "peak_demand_error": None,
            "operational_cost_estimate": None,
            "serving_readiness": None,
        },
        "limitations": [
            "No final metric or business benefit is asserted until full archived forecast coverage is evaluated.",
            "Weather availability is based on the conservative model-run-plus-six-hours policy, not a provider publication guarantee.",
        ],
    }
