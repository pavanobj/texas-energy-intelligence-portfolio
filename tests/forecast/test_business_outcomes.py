from __future__ import annotations

from texas_energy.forecast.business_outcomes import pending_business_outcome_report


def test_pending_business_report_does_not_invent_metrics() -> None:
    report = pending_business_outcome_report(catalog_path="catalog.json", evaluation_path="evaluation")
    assert report["status"] == "pending_validated_full_archive_evaluation"
    assert report["outcomes"]["forecast_accuracy"] is None
    assert report["outcomes"]["operational_cost_estimate"] is None
