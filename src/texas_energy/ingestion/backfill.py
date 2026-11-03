"""Bounded, restart-safe historical ingestion using the existing Bronze contracts."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
import json
from math import ceil
import os
from pathlib import Path
from typing import Protocol

from .eia import EiaDemandQuery
from .errors import BackfillExecutionError
from .open_meteo import TexasCity
from .orchestration import BronzeIngestionResult


DEFAULT_EIA_BATCH_DAYS = 7
DEFAULT_WEATHER_BATCH_DAYS = 7
DEFAULT_EIA_PAGE_SIZE = 168


@dataclass(frozen=True)
class HistoricalWindow:
    """An inclusive UTC-calendar batch used for one source request."""

    start_date: date
    end_date: date

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("Historical window end_date must not precede start_date.")

    @property
    def days(self) -> int:
        return (self.end_date - self.start_date).days + 1

    def eia_query(self, *, page_size: int) -> EiaDemandQuery:
        return EiaDemandQuery(
            start=datetime.combine(self.start_date, time.min, tzinfo=timezone.utc),
            end=datetime.combine(self.end_date, time(hour=23), tzinfo=timezone.utc),
            page_size=page_size,
        )


@dataclass(frozen=True)
class HistoricalBackfillPlan:
    """Validated source range and deliberately small request batch sizes."""

    start_date: date
    end_date: date
    eia_batch_days: int = DEFAULT_EIA_BATCH_DAYS
    weather_batch_days: int = DEFAULT_WEATHER_BATCH_DAYS
    eia_page_size: int = DEFAULT_EIA_PAGE_SIZE

    def __post_init__(self) -> None:
        if self.end_date < self.start_date:
            raise ValueError("end_date must not precede start_date.")
        for name, value, maximum in (
            ("eia_batch_days", self.eia_batch_days, 31),
            ("weather_batch_days", self.weather_batch_days, 31),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= maximum:
                raise ValueError(f"{name} must be an integer between 1 and {maximum}.")
        if isinstance(self.eia_page_size, bool) or not isinstance(self.eia_page_size, int):
            raise ValueError("eia_page_size must be an integer.")
        # EiaDemandQuery owns the authoritative page-size range validation.
        HistoricalWindow(self.start_date, self.start_date).eia_query(page_size=self.eia_page_size)

    def eia_windows(self) -> Iterator[HistoricalWindow]:
        yield from _windows(self.start_date, self.end_date, self.eia_batch_days)

    def weather_windows(self) -> Iterator[HistoricalWindow]:
        yield from _windows(self.start_date, self.end_date, self.weather_batch_days)

    def estimate(
        self,
        *,
        include_eia: bool,
        weather_cities: tuple[TexasCity, ...],
    ) -> "BackfillWorkEstimate":
        """Return a deterministic, network-free estimate for operator review.

        EIA normally supplies ``response.total`` (verified during Phase 1B), so
        its page count is also the expected HTTP-request count. If that metadata
        is absent and a requested window fills a page exactly, the defensive EIA
        client makes one additional empty-page request before it can prove the
        response is complete. The estimate exposes that bounded upper limit.
        """

        if not include_eia and not weather_cities:
            raise ValueError("At least one EIA or historical weather source must be selected.")
        if len(set(weather_cities)) != len(weather_cities):
            raise ValueError("Historical weather cities must not contain duplicates.")

        eia_windows = tuple(self.eia_windows()) if include_eia else ()
        weather_windows = tuple(self.weather_windows()) if weather_cities else ()
        eia_page_count = sum(ceil(window.days * 24 / self.eia_page_size) for window in eia_windows)
        eia_extra_empty_page_requests = sum(
            1 for window in eia_windows if (window.days * 24) % self.eia_page_size == 0
        )
        weather_request_count = len(weather_cities) * len(weather_windows)
        return BackfillWorkEstimate(
            start_date=self.start_date,
            end_date=self.end_date,
            utc_hours=(self.end_date - self.start_date).days * 24 + 24,
            weather_cities=tuple(city.slug for city in weather_cities),
            eia_window_count=len(eia_windows),
            eia_expected_page_count=eia_page_count,
            eia_max_extra_empty_page_requests=eia_extra_empty_page_requests,
            weather_window_count_per_city=len(weather_windows),
            weather_request_count=weather_request_count,
        )


@dataclass(frozen=True)
class BackfillWorkEstimate:
    """Bounded work counts; all values are derived without credentials or I/O."""

    start_date: date
    end_date: date
    utc_hours: int
    weather_cities: tuple[str, ...]
    eia_window_count: int
    eia_expected_page_count: int
    eia_max_extra_empty_page_requests: int
    weather_window_count_per_city: int
    weather_request_count: int

    @property
    def weather_hourly_observations(self) -> int:
        return self.utc_hours * len(self.weather_cities)

    @property
    def expected_bronze_objects(self) -> int:
        return self.eia_expected_page_count + self.weather_request_count

    @property
    def operation_count(self) -> int:
        return self.eia_window_count + self.weather_request_count

    @property
    def expected_http_requests(self) -> int:
        return self.eia_expected_page_count + self.weather_request_count

    @property
    def maximum_http_requests(self) -> int:
        return self.expected_http_requests + self.eia_max_extra_empty_page_requests


class HistoricalIngestionOperations(Protocol):
    """Narrow orchestration surface used by a historical backfill."""

    def ingest_eia_ercot_demand(
        self,
        query: EiaDemandQuery,
        *,
        allow_empty: bool = False,
        require_complete_hours: bool = False,
    ) -> tuple[BronzeIngestionResult, ...]:
        """Persist each validated EIA page to immutable Bronze storage."""

    def ingest_open_meteo_historical(
        self,
        *,
        city: TexasCity,
        start_date: date,
        end_date: date,
        require_complete_hours: bool = False,
    ) -> BronzeIngestionResult:
        """Persist one validated city's historical weather capture."""


@dataclass(frozen=True)
class BackfillProgress:
    """Safe, restart-useful facts emitted after every completed request window."""

    source: str
    city: str | None
    window: HistoricalWindow
    result_count: int
    already_existing_count: int
    operation_index: int = 0
    operation_count: int = 0

    @property
    def resume_key(self) -> tuple[str, str | None, date, date]:
        """The stable identity of one source/window operation."""

        return (self.source, self.city, self.window.start_date, self.window.end_date)


class BackfillResumeLedger:
    """Durable local record of fully completed historical request windows.

    The ledger is deliberately append-only JSON Lines rather than an assertion
    about S3 state. A record is appended only after Bronze persistence and the
    complete-hours validation both succeed. If a process stops before appending
    it, replay is safe because Bronze writes are immutable conditional puts.
    A malformed/truncated ledger fails closed: no window is skipped.
    """

    _VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._completed = self._load(path)

    def contains(self, *, source: str, city: str | None, window: HistoricalWindow) -> bool:
        return (source, city, window.start_date, window.end_date) in self._completed

    def record(self, progress: BackfillProgress) -> None:
        payload = {
            "version": self._VERSION,
            "source": progress.source,
            "city": progress.city,
            "start_date": progress.window.start_date.isoformat(),
            "end_date": progress.window.end_date.isoformat(),
        }
        key = progress.resume_key
        if key in self._completed:
            return
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())
        self._completed.add(key)

    @classmethod
    def _load(cls, path: Path) -> set[tuple[str, str | None, date, date]]:
        if not path.exists():
            return set()
        completed: set[tuple[str, str | None, date, date]] = set()
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict) or value.get("version") != cls._VERSION:
                        raise ValueError
                    source, city = value["source"], value["city"]
                    if not isinstance(source, str) or city is not None and not isinstance(city, str):
                        raise ValueError
                    completed.add((source, city, date.fromisoformat(value["start_date"]), date.fromisoformat(value["end_date"])))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise BackfillExecutionError(
                        f"Historical resume ledger is invalid at line {line_number}; no windows were skipped."
                    ) from error
        return completed


@dataclass(frozen=True)
class BackfillIncompleteWindow:
    """One failed source window, explicitly distinct from a completion record."""

    source: str
    city: str | None
    window: HistoricalWindow
    failure_type: str
    transport_phase: str | None = None

    @property
    def resume_key(self) -> tuple[str, str | None, date, date]:
        return (self.source, self.city, self.window.start_date, self.window.end_date)


class BackfillIncompleteLedger:
    """Append-only local audit of windows that require a later retry.

    This ledger never authorizes a skip.  Only :class:`BackfillResumeLedger`
    can do that, after a full validation and immutable Bronze write succeed.
    """

    _VERSION = 1

    def __init__(self, path: Path) -> None:
        self._path = path
        self._incomplete = self._load(path)

    def record(self, failure: BackfillIncompleteWindow) -> None:
        existing = self._incomplete.get(failure.resume_key)
        if existing == failure:
            return
        self._append(
            {
                "version": self._VERSION,
                "status": "incomplete",
                "source": failure.source,
                "city": failure.city,
                "start_date": failure.window.start_date.isoformat(),
                "end_date": failure.window.end_date.isoformat(),
                "failure_type": failure.failure_type,
                "transport_phase": failure.transport_phase,
            }
        )
        self._incomplete[failure.resume_key] = failure

    def resolve(self, progress: BackfillProgress) -> None:
        key = progress.resume_key
        if key not in self._incomplete:
            return
        self._append(
            {
                "version": self._VERSION,
                "status": "resolved",
                "source": progress.source,
                "city": progress.city,
                "start_date": progress.window.start_date.isoformat(),
                "end_date": progress.window.end_date.isoformat(),
            }
        )
        del self._incomplete[key]

    def pending(self) -> tuple[BackfillIncompleteWindow, ...]:
        return tuple(
            self._incomplete[key]
            for key in sorted(self._incomplete, key=lambda value: (value[0], value[1] or "", value[2], value[3]))
        )

    def _append(self, payload: dict[str, object]) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")) + "\n"
        with self._path.open("a", encoding="utf-8") as stream:
            stream.write(encoded)
            stream.flush()
            os.fsync(stream.fileno())

    @classmethod
    def _load(cls, path: Path) -> dict[tuple[str, str | None, date, date], BackfillIncompleteWindow]:
        if not path.exists():
            return {}
        incomplete: dict[tuple[str, str | None, date, date], BackfillIncompleteWindow] = {}
        with path.open(encoding="utf-8") as stream:
            for line_number, line in enumerate(stream, start=1):
                try:
                    value = json.loads(line)
                    if not isinstance(value, dict) or value.get("version") != cls._VERSION:
                        raise ValueError
                    source, city = value["source"], value["city"]
                    if not isinstance(source, str) or city is not None and not isinstance(city, str):
                        raise ValueError
                    window = HistoricalWindow(date.fromisoformat(value["start_date"]), date.fromisoformat(value["end_date"]))
                    key = (source, city, window.start_date, window.end_date)
                    status = value["status"]
                    if status == "resolved":
                        incomplete.pop(key, None)
                    elif status == "incomplete":
                        failure_type, transport_phase = value["failure_type"], value.get("transport_phase")
                        if not isinstance(failure_type, str) or transport_phase is not None and not isinstance(transport_phase, str):
                            raise ValueError
                        incomplete[key] = BackfillIncompleteWindow(
                            source, city, window, failure_type, transport_phase
                        )
                    else:
                        raise ValueError
                except (KeyError, TypeError, ValueError, json.JSONDecodeError) as error:
                    raise BackfillExecutionError(
                        f"Historical incomplete ledger is invalid at line {line_number}; no windows were skipped."
                    ) from error
        return incomplete


@dataclass(frozen=True)
class HistoricalBackfillReport:
    """Completion facts; an exception is raised instead of silently omitting failures."""

    plan: HistoricalBackfillPlan
    results: tuple[BronzeIngestionResult, ...]
    completed_windows: tuple[BackfillProgress, ...]
    incomplete_windows: tuple[BackfillIncompleteWindow, ...] = ()

    @property
    def created_objects(self) -> int:
        return sum(not result.already_exists for result in self.results)

    @property
    def existing_objects(self) -> int:
        return sum(result.already_exists for result in self.results)


class HistoricalBackfillRunner:
    """Run serialized, resumable backfills without hiding incomplete windows.

    Re-running the same plan is safe: the existing writer targets deterministic,
    immutable objects and reports objects already present. A source may legitimately
    revise historical bytes; that produces a distinct Bronze object and Silver later
    reconciles the business key deterministically.
    """

    def __init__(self, operations: HistoricalIngestionOperations) -> None:
        self._operations = operations

    def run(
        self,
        plan: HistoricalBackfillPlan,
        *,
        include_eia: bool,
        weather_cities: tuple[TexasCity, ...],
        progress: Callable[[BackfillProgress], None] | None = None,
        resume_ledger: BackfillResumeLedger | None = None,
        incomplete_ledger: BackfillIncompleteLedger | None = None,
        continue_on_failure: bool = False,
    ) -> HistoricalBackfillReport:
        if not include_eia and not weather_cities:
            raise ValueError("At least one EIA or historical weather source must be selected.")
        if len(set(weather_cities)) != len(weather_cities):
            raise ValueError("Historical weather cities must not contain duplicates.")

        completed: list[BackfillProgress] = []
        results: list[BronzeIngestionResult] = []
        incomplete: list[BackfillIncompleteWindow] = []
        operation_count = plan.estimate(include_eia=include_eia, weather_cities=weather_cities).operation_count
        operation_index = 0
        current_source: str | None = None
        current_city: str | None = None
        current_window: HistoricalWindow | None = None
        try:
            if include_eia:
                for window in plan.eia_windows():
                    operation_index += 1
                    if resume_ledger is not None and resume_ledger.contains(source="eia", city=None, window=window):
                        continue
                    current_source, current_city, current_window = "eia", None, window
                    try:
                        window_results = self._operations.ingest_eia_ercot_demand(
                            window.eia_query(page_size=plan.eia_page_size),
                            require_complete_hours=True,
                        )
                        _require_complete_eia_window(window.eia_query(page_size=plan.eia_page_size), window_results)
                    except Exception as error:
                        failure = _incomplete_failure("eia", None, window, error)
                        if not continue_on_failure:
                            raise _contextual_failure("eia", None, window, error) from None
                        incomplete.append(failure)
                        if incomplete_ledger is not None:
                            incomplete_ledger.record(failure)
                        current_source = current_window = None
                        continue
                    progress_item = _record_progress(
                        completed,
                        results,
                        "eia",
                        None,
                        window,
                        window_results,
                        progress,
                        operation_index=operation_index,
                        operation_count=operation_count,
                    )
                    if resume_ledger is not None:
                        resume_ledger.record(progress_item)
                    if incomplete_ledger is not None:
                        incomplete_ledger.resolve(progress_item)
                    current_source = current_window = None
            for city in weather_cities:
                for window in plan.weather_windows():
                    operation_index += 1
                    if resume_ledger is not None and resume_ledger.contains(
                        source="open_meteo", city=city.slug, window=window
                    ):
                        continue
                    current_source, current_city, current_window = "open_meteo", city.slug, window
                    try:
                        result = self._operations.ingest_open_meteo_historical(
                            city=city,
                            start_date=window.start_date,
                            end_date=window.end_date,
                            require_complete_hours=True,
                        )
                        _require_complete_weather_window(window, result)
                    except Exception as error:
                        failure = _incomplete_failure("open_meteo", city.slug, window, error)
                        if not continue_on_failure:
                            raise _contextual_failure("open_meteo", city.slug, window, error) from None
                        incomplete.append(failure)
                        if incomplete_ledger is not None:
                            incomplete_ledger.record(failure)
                        current_source = current_window = None
                        current_city = None
                        continue
                    progress_item = _record_progress(
                        completed,
                        results,
                        "open_meteo",
                        city.slug,
                        window,
                        (result,),
                        progress,
                        operation_index=operation_index,
                        operation_count=operation_count,
                    )
                    if resume_ledger is not None:
                        resume_ledger.record(progress_item)
                    if incomplete_ledger is not None:
                        incomplete_ledger.resolve(progress_item)
                    current_source = current_window = None
                    current_city = None
        except BackfillExecutionError as error:
            if current_source is None or current_window is None:
                raise
            raise _contextual_failure(current_source, current_city, current_window, error) from None
        except Exception as error:
            if current_source is None or current_window is None:
                raise BackfillExecutionError(
                    "Historical backfill stopped before completion; rerun the same plan or resume at the first unreported window."
                ) from None
            raise _contextual_failure(current_source, current_city, current_window, error) from None
        return HistoricalBackfillReport(plan, tuple(results), tuple(completed), tuple(incomplete))


def _incomplete_failure(
    source: str,
    city: str | None,
    window: HistoricalWindow,
    error: Exception,
) -> BackfillIncompleteWindow:
    return BackfillIncompleteWindow(
        source=source,
        city=city,
        window=window,
        failure_type=type(error).__name__,
        transport_phase=getattr(error, "phase", None) if type(error).__name__ == "TransportError" else None,
    )


def _contextual_failure(
    source: str,
    city: str | None,
    window: HistoricalWindow,
    error: Exception,
) -> BackfillExecutionError:
    """Expose only safe operation identity and exception class to operators."""

    location = city or "ercot"
    message = (
        "Historical backfill stopped before completion "
        f"at source={source} city={location} window={window.start_date.isoformat()}..{window.end_date.isoformat()} "
        f"failure_type={type(error).__name__}; rerun the same plan or resume at the first unreported window."
    )
    if isinstance(error, BackfillExecutionError):
        # These errors originate exclusively in this module and contain only
        # contract facts, never provider URLs, raw payloads, or credentials.
        message = f"{message} Contract detail: {error}"
    elif type(error).__name__ == "TransportError":
        phase = getattr(error, "phase", None)
        if phase is not None:
            message = f"{message} transport_phase={phase}."
    return BackfillExecutionError(message)


def _windows(start_date: date, end_date: date, batch_days: int) -> Iterator[HistoricalWindow]:
    current = start_date
    while current <= end_date:
        batch_end = min(current + timedelta(days=batch_days - 1), end_date)
        yield HistoricalWindow(current, batch_end)
        current = batch_end + timedelta(days=1)


def _record_progress(
    completed: list[BackfillProgress],
    results: list[BronzeIngestionResult],
    source: str,
    city: str | None,
    window: HistoricalWindow,
    window_results: tuple[BronzeIngestionResult, ...],
    progress: Callable[[BackfillProgress], None] | None,
    *,
    operation_index: int,
    operation_count: int,
) -> BackfillProgress:
    progress_item = BackfillProgress(
        source=source,
        city=city,
        window=window,
        result_count=len(window_results),
        already_existing_count=sum(result.already_exists for result in window_results),
        operation_index=operation_index,
        operation_count=operation_count,
    )
    results.extend(window_results)
    completed.append(progress_item)
    if progress is not None:
        progress(progress_item)
    return progress_item


def _require_complete_eia_window(
    query: EiaDemandQuery,
    results: tuple[BronzeIngestionResult, ...],
) -> None:
    if not results:
        raise BackfillExecutionError("EIA historical backfill response was empty for a requested window.")
    ordered = sorted(results, key=lambda result: result.source_time_range.start)
    expected = query.start
    for result in ordered:
        source_range = result.source_time_range
        expected_count = int((source_range.end - source_range.start).total_seconds() / 3600) + 1
        if result.record_count != expected_count or source_range.start != expected:
            raise BackfillExecutionError("EIA historical backfill response did not cover every requested UTC hour.")
        expected = source_range.end + timedelta(hours=1)
    if expected != query.end + timedelta(hours=1):
        raise BackfillExecutionError("EIA historical backfill response did not cover every requested UTC hour.")


def _require_complete_weather_window(window: HistoricalWindow, result: BronzeIngestionResult) -> None:
    """Reject a protocol implementation that claims a weather window is complete when it is not."""

    expected_start = datetime.combine(window.start_date, time.min, tzinfo=timezone.utc)
    expected_end = datetime.combine(window.end_date, time(hour=23), tzinfo=timezone.utc)
    expected_count = window.days * 24
    source_range = result.source_time_range
    if (
        result.provider != "open_meteo"
        or result.dataset != "historical-weather"
        or result.record_count != expected_count
        or source_range.start != expected_start
        or source_range.end != expected_end
    ):
        raise BackfillExecutionError("Open-Meteo historical response did not cover every requested UTC hour.")
