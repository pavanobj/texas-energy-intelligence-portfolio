"""Mock-only tests for bounded historical backfill planning and recovery behavior."""

from __future__ import annotations

from datetime import date, datetime, timezone

import pytest

from texas_energy.ingestion.backfill import (
    BackfillIncompleteLedger,
    BackfillResumeLedger,
    HistoricalBackfillPlan,
    HistoricalBackfillRunner,
)
from texas_energy.ingestion.bronze import BronzeSourceTimeRange
from texas_energy.ingestion.eia import EiaDemandQuery
from texas_energy.ingestion.errors import BackfillExecutionError
from texas_energy.ingestion.open_meteo import TexasCity
from texas_energy.ingestion.orchestration import BronzeIngestionResult


def _result(
    *,
    provider: str,
    dataset: str,
    already_exists: bool = False,
    start: datetime | None = None,
    end: datetime | None = None,
    record_count: int = 1,
) -> BronzeIngestionResult:
    return BronzeIngestionResult(
        provider=provider,
        dataset=dataset,
        object_key=f"bronze/{provider}/{dataset}.json",
        record_count=record_count,
        source_time_range=BronzeSourceTimeRange(
            start=start or datetime(2024, 1, 1, tzinfo=timezone.utc),
            end=end or datetime(2024, 1, 1, tzinfo=timezone.utc),
        ),
        already_exists=already_exists,
    )


class FakeOperations:
    def __init__(self, *, fail_eia_call: int | None = None) -> None:
        self.eia_queries: list[EiaDemandQuery] = []
        self.weather_calls: list[tuple[TexasCity, date, date]] = []
        self.fail_eia_call = fail_eia_call

    def ingest_eia_ercot_demand(
        self, query: EiaDemandQuery, *, allow_empty: bool = False, require_complete_hours: bool = False
    ) -> tuple[BronzeIngestionResult, ...]:
        assert allow_empty is False
        assert require_complete_hours is True
        self.eia_queries.append(query)
        if self.fail_eia_call == len(self.eia_queries):
            raise RuntimeError("mock source failure")
        hours = int((query.end - query.start).total_seconds() / 3600) + 1
        return (
            _result(
                provider="eia",
                dataset="electricity/rto/region-data",
                already_exists=len(self.eia_queries) == 1,
                start=query.start,
                end=query.end,
                record_count=hours,
            ),
        )

    def ingest_open_meteo_historical(
        self, *, city: TexasCity, start_date: date, end_date: date, require_complete_hours: bool = False
    ) -> BronzeIngestionResult:
        self.weather_calls.append((city, start_date, end_date))
        assert require_complete_hours is True
        return _result(
            provider="open_meteo",
            dataset="historical-weather",
            start=datetime.combine(start_date, datetime.min.time(), tzinfo=timezone.utc),
            end=datetime.combine(end_date, datetime.max.time(), tzinfo=timezone.utc).replace(
                hour=23, minute=0, second=0, microsecond=0
            ),
            record_count=((end_date - start_date).days + 1) * 24,
        )


def test_eia_backfill_uses_inclusive_non_overlapping_utc_windows_and_reports_duplicates() -> None:
    operations = FakeOperations()
    progress = []
    report = HistoricalBackfillRunner(operations).run(
        HistoricalBackfillPlan(
            start_date=date(2024, 1, 1),
            end_date=date(2024, 1, 5),
            eia_batch_days=2,
            weather_batch_days=1,
            eia_page_size=24,
        ),
        include_eia=True,
        weather_cities=(),
        progress=progress.append,
    )

    assert [(query.start, query.end) for query in operations.eia_queries] == [
        (datetime(2024, 1, 1, tzinfo=timezone.utc), datetime(2024, 1, 2, 23, tzinfo=timezone.utc)),
        (datetime(2024, 1, 3, tzinfo=timezone.utc), datetime(2024, 1, 4, 23, tzinfo=timezone.utc)),
        (datetime(2024, 1, 5, tzinfo=timezone.utc), datetime(2024, 1, 5, 23, tzinfo=timezone.utc)),
    ]
    assert report.existing_objects == 1
    assert report.created_objects == 2
    assert [(item.window.start_date, item.window.end_date) for item in progress] == [
        (date(2024, 1, 1), date(2024, 1, 2)),
        (date(2024, 1, 3), date(2024, 1, 4)),
        (date(2024, 1, 5), date(2024, 1, 5)),
    ]


def test_weather_backfill_supports_each_configured_city_without_eia_work() -> None:
    operations = FakeOperations()
    cities = tuple(TexasCity)
    HistoricalBackfillRunner(operations).run(
        HistoricalBackfillPlan(
            start_date=date(2024, 2, 1),
            end_date=date(2024, 2, 2),
            weather_batch_days=1,
        ),
        include_eia=False,
        weather_cities=cities,
    )

    assert operations.eia_queries == []
    assert len(operations.weather_calls) == len(cities) * 2
    assert {city for city, _, _ in operations.weather_calls} == set(cities)


def test_backfill_fails_loudly_after_partial_completion_so_a_rerun_can_resume_immutably() -> None:
    operations = FakeOperations(fail_eia_call=2)
    progress = []

    with pytest.raises(BackfillExecutionError, match="stopped before completion"):
        HistoricalBackfillRunner(operations).run(
            HistoricalBackfillPlan(
                start_date=date(2024, 1, 1),
                end_date=date(2024, 1, 3),
                eia_batch_days=1,
            ),
            include_eia=True,
            weather_cities=(),
            progress=progress.append,
        )

    assert len(operations.eia_queries) == 2
    assert len(progress) == 1


def test_backfill_failure_identifies_only_the_safe_operation_context() -> None:
    class SecretFailureOperations(FakeOperations):
        def ingest_eia_ercot_demand(
            self, query: EiaDemandQuery, *, allow_empty: bool = False, require_complete_hours: bool = False
        ) -> tuple[BronzeIngestionResult, ...]:
            raise RuntimeError("https://api.example.invalid/data?api_key=must-not-appear")

    with pytest.raises(BackfillExecutionError) as raised:
        HistoricalBackfillRunner(SecretFailureOperations()).run(
            HistoricalBackfillPlan(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1)),
            include_eia=True,
            weather_cities=(),
        )

    message = str(raised.value)
    assert "source=eia city=ercot window=2024-01-01..2024-01-01" in message
    assert "failure_type=RuntimeError" in message
    assert "api_key" not in message
    assert "example.invalid" not in message


def test_resume_ledger_skips_only_windows_recorded_after_success(tmp_path) -> None:
    ledger_path = tmp_path / "resume.jsonl"
    plan = HistoricalBackfillPlan(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 3), eia_batch_days=1
    )
    failing = FakeOperations(fail_eia_call=2)
    with pytest.raises(BackfillExecutionError):
        HistoricalBackfillRunner(failing).run(
            plan, include_eia=True, weather_cities=(), resume_ledger=BackfillResumeLedger(ledger_path)
        )

    resumed = FakeOperations()
    report = HistoricalBackfillRunner(resumed).run(
        plan, include_eia=True, weather_cities=(), resume_ledger=BackfillResumeLedger(ledger_path)
    )

    # The first operation was durably recorded after success; only the two
    # unfinished UTC windows reissue a provider request.
    assert [(query.start.date(), query.end.date()) for query in resumed.eia_queries] == [
        (date(2024, 1, 2), date(2024, 1, 2)),
        (date(2024, 1, 3), date(2024, 1, 3)),
    ]
    assert len(report.completed_windows) == 2


def test_continue_on_failure_records_incomplete_window_and_runs_later_windows(tmp_path) -> None:
    plan = HistoricalBackfillPlan(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 3), eia_batch_days=1
    )
    incomplete = BackfillIncompleteLedger(tmp_path / "incomplete.jsonl")
    operations = FakeOperations(fail_eia_call=2)

    report = HistoricalBackfillRunner(operations).run(
        plan,
        include_eia=True,
        weather_cities=(),
        continue_on_failure=True,
        incomplete_ledger=incomplete,
    )

    assert len(operations.eia_queries) == 3
    assert [(item.window.start_date, item.window.end_date) for item in report.incomplete_windows] == [
        (date(2024, 1, 2), date(2024, 1, 2))
    ]
    assert [(item.window.start_date, item.window.end_date) for item in incomplete.pending()] == [
        (date(2024, 1, 2), date(2024, 1, 2))
    ]


def test_incomplete_window_is_resolved_only_after_later_successful_persistence(tmp_path) -> None:
    plan = HistoricalBackfillPlan(
        start_date=date(2024, 1, 1), end_date=date(2024, 1, 2), eia_batch_days=1
    )
    completed_path = tmp_path / "resume.jsonl"
    incomplete_path = tmp_path / "incomplete.jsonl"
    HistoricalBackfillRunner(FakeOperations(fail_eia_call=2)).run(
        plan,
        include_eia=True,
        weather_cities=(),
        continue_on_failure=True,
        resume_ledger=BackfillResumeLedger(completed_path),
        incomplete_ledger=BackfillIncompleteLedger(incomplete_path),
    )

    resumed = FakeOperations()
    HistoricalBackfillRunner(resumed).run(
        plan,
        include_eia=True,
        weather_cities=(),
        continue_on_failure=True,
        resume_ledger=BackfillResumeLedger(completed_path),
        incomplete_ledger=BackfillIncompleteLedger(incomplete_path),
    )

    assert [(query.start.date(), query.end.date()) for query in resumed.eia_queries] == [
        (date(2024, 1, 2), date(2024, 1, 2))
    ]
    assert BackfillIncompleteLedger(incomplete_path).pending() == ()


def test_eia_failure_does_not_block_independent_weather_windows(tmp_path) -> None:
    operations = FakeOperations(fail_eia_call=1)
    report = HistoricalBackfillRunner(operations).run(
        HistoricalBackfillPlan(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1)),
        include_eia=True,
        weather_cities=(TexasCity.DALLAS,),
        continue_on_failure=True,
        incomplete_ledger=BackfillIncompleteLedger(tmp_path / "incomplete.jsonl"),
    )

    assert len(report.incomplete_windows) == 1
    assert report.incomplete_windows[0].source == "eia"
    assert len(operations.weather_calls) == 1


def test_invalid_resume_ledger_fails_closed_before_any_provider_call(tmp_path) -> None:
    ledger_path = tmp_path / "resume.jsonl"
    ledger_path.write_text("not-json\n", encoding="utf-8")
    with pytest.raises(BackfillExecutionError, match="no windows were skipped"):
        BackfillResumeLedger(ledger_path)


def test_backfill_rejects_empty_source_selection_and_unsafe_batch_sizes() -> None:
    with pytest.raises(ValueError, match="At least one"):
        HistoricalBackfillRunner(FakeOperations()).run(
            HistoricalBackfillPlan(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1)),
            include_eia=False,
            weather_cities=(),
        )
    with pytest.raises(ValueError, match="eia_batch_days"):
        HistoricalBackfillPlan(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1), eia_batch_days=32)


def test_backfill_dry_run_estimate_accounts_for_pagination_boundaries_and_city_hours() -> None:
    estimate = HistoricalBackfillPlan(
        start_date=date(2024, 1, 1),
        end_date=date(2024, 1, 14),
        eia_batch_days=7,
        weather_batch_days=7,
        eia_page_size=168,
    ).estimate(include_eia=True, weather_cities=tuple(TexasCity))

    assert estimate.utc_hours == 336
    assert estimate.eia_window_count == 2
    assert estimate.eia_expected_page_count == 2
    assert estimate.eia_max_extra_empty_page_requests == 2
    assert estimate.weather_request_count == 10
    assert estimate.weather_hourly_observations == 336 * 5
    assert estimate.expected_bronze_objects == 12
    assert estimate.expected_http_requests == 12
    assert estimate.maximum_http_requests == 14


def test_backfill_rejects_a_partial_eia_window_instead_of_claiming_completion() -> None:
    class PartialEiaOperations(FakeOperations):
        def ingest_eia_ercot_demand(
            self, query: EiaDemandQuery, *, allow_empty: bool = False, require_complete_hours: bool = False
        ) -> tuple[BronzeIngestionResult, ...]:
            return (
                _result(
                    provider="eia",
                    dataset="electricity/rto/region-data",
                    start=query.start,
                    end=query.end,
                    record_count=1,
                ),
            )

    with pytest.raises(BackfillExecutionError, match="did not cover every requested UTC hour"):
        HistoricalBackfillRunner(PartialEiaOperations()).run(
            HistoricalBackfillPlan(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1)),
            include_eia=True,
            weather_cities=(),
        )


def test_backfill_rejects_a_partial_weather_window_before_reporting_completion() -> None:
    class PartialWeatherOperations(FakeOperations):
        def ingest_open_meteo_historical(
            self, *, city: TexasCity, start_date: date, end_date: date, require_complete_hours: bool = False
        ) -> BronzeIngestionResult:
            return _result(provider="open_meteo", dataset="historical-weather", record_count=1)

    with pytest.raises(BackfillExecutionError, match="Open-Meteo historical response did not cover every requested UTC hour"):
        HistoricalBackfillRunner(PartialWeatherOperations()).run(
            HistoricalBackfillPlan(start_date=date(2024, 1, 1), end_date=date(2024, 1, 1)),
            include_eia=False,
            weather_cities=(TexasCity.DALLAS,),
        )
