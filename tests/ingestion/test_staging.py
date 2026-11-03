from datetime import date

from datetime import datetime, timezone

from texas_energy.data.coverage import HistoricalCoverageAudit
from texas_energy.ingestion.staging import build_monthly_stage_plan, classify_coverage


def test_monthly_plan_skips_only_explicitly_accepted_january() -> None:
    plan = build_monthly_stage_plan(
        start_date=date(2020, 1, 1),
        end_date=date(2020, 3, 31),
        completed_months=frozenset({"2020-01"}),
    )

    assert [stage.identifier for stage in plan.skipped_completed] == ["2020-01"]
    assert [(stage.start_date, stage.end_date) for stage in plan.pending] == [
        (date(2020, 2, 1), date(2020, 2, 29)),
        (date(2020, 3, 1), date(2020, 3, 31)),
    ]


def test_coverage_failures_quarantine_only_the_affected_stage() -> None:
    audit = HistoricalCoverageAudit(
        start_date=date(2021, 11, 7),
        end_date=date(2021, 11, 7),
        expected_demand_hours=24,
        observed_demand_records=23,
        expected_weather_city_hours=120,
        observed_historical_weather_records=120,
        missing_demand_hours=(datetime(2021, 11, 7, 8, tzinfo=timezone.utc),),
        missing_weather_city_hours=(),
        duplicate_demand_hours=(),
        duplicate_weather_city_hours=(),
        source_revisions=(),
        unexpected_demand_hours=(),
        unexpected_weather_city_hours=(),
    )

    disposition = classify_coverage(audit)

    assert not disposition.accepted
    assert "quarantine required" in disposition.reason
    assert "missing demand hours=1" in disposition.reason
