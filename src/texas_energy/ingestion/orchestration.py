"""Local composition of validated provider clients and immutable Bronze persistence."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Protocol

from .bronze import BronzeS3Writer, BronzeSourceTimeRange
from .eia import EiaDemandFetch, EiaDemandPage, EiaDemandQuery
from .errors import ContractError, EmptySourceResponseError, SourceDataUnavailableError
from .open_meteo import TexasCity, WeatherResponse
from .raw import RawCapture


class EiaDemandFetcher(Protocol):
    """The EIA client operation needed by local orchestration."""

    def fetch(self, query: EiaDemandQuery) -> EiaDemandFetch:
        """Fetch a fully validated ERCOT hourly-demand result."""


class WeatherFetcher(Protocol):
    """The Open-Meteo client operations needed by local orchestration."""

    def fetch_historical(
        self,
        city: TexasCity,
        *,
        start_date: date,
        end_date: date,
    ) -> WeatherResponse:
        """Fetch one validated historical weather response."""

    def fetch_forecast(self, city: TexasCity, *, forecast_hours: int = 48) -> WeatherResponse:
        """Fetch one validated forecast weather response."""

    def fetch_issued_ifs(
        self, city: TexasCity, *, model_run_utc: datetime, forecast_hours: int = 48
    ) -> WeatherResponse:
        """Fetch one explicit IFS Single Runs response."""


@dataclass(frozen=True)
class BronzeIngestionResult:
    """Safe facts about one captured provider response persisted to Bronze."""

    provider: str
    dataset: str
    object_key: str
    record_count: int
    source_time_range: BronzeSourceTimeRange
    already_exists: bool
    page_offset: int | None = None


@dataclass(frozen=True)
class _PendingBronzeWrite:
    """Fully derived persistence input, built before any provider object is written."""

    capture: RawCapture
    source_time_range: BronzeSourceTimeRange
    units: Mapping[str, str]
    record_count: int
    page_offset: int | None


class IngestionOrchestrator:
    """Connect validated source clients to Bronze without source-specific S3 behavior."""

    def __init__(
        self,
        *,
        eia_client: EiaDemandFetcher,
        weather_client: WeatherFetcher,
        bronze_writer: BronzeS3Writer,
    ) -> None:
        self._eia_client = eia_client
        self._weather_client = weather_client
        self._bronze_writer = bronze_writer

    def ingest_eia_ercot_demand(
        self,
        query: EiaDemandQuery,
        *,
        allow_empty: bool = False,
        require_complete_hours: bool = False,
    ) -> tuple[BronzeIngestionResult, ...]:
        """Fetch EIA once and persist each non-empty validated page independently."""

        fetched = self._eia_client.fetch(query)
        if not fetched.records:
            if allow_empty:
                return ()
            raise EmptySourceResponseError(
                "EIA returned zero ERCOT demand records; allow_empty=True is required to accept this result."
            )
        pending = tuple(_pending_eia_page(page) for page in fetched.pages if page.records)
        if require_complete_hours:
            _require_complete_eia_query(query, pending)
        return self._persist(pending)

    def ingest_open_meteo_historical(
        self,
        *,
        city: TexasCity,
        start_date: date,
        end_date: date,
        require_complete_hours: bool = False,
    ) -> BronzeIngestionResult:
        """Fetch one city's historical weather and persist its validated capture."""

        response = self._weather_client.fetch_historical(
            city,
            start_date=start_date,
            end_date=end_date,
        )
        _require_available_historical_weather(response)
        if require_complete_hours:
            _require_complete_historical_weather(response, start_date=start_date, end_date=end_date)
        return self._persist((_pending_weather_response(response),))[0]

    def ingest_open_meteo_historical_cities(
        self,
        *,
        cities: tuple[TexasCity, ...],
        start_date: date,
        end_date: date,
        require_complete_hours: bool = False,
    ) -> tuple[BronzeIngestionResult, ...]:
        """Persist a five-city historical window only when every city is usable.

        Fetch and validate the whole window before the first Bronze write so a
        provider availability gap cannot leave a partial city set behind.
        """

        responses = tuple(
            self._weather_client.fetch_historical(city, start_date=start_date, end_date=end_date)
            for city in cities
        )
        for response in responses:
            _require_available_historical_weather(response)
            if require_complete_hours:
                _require_complete_historical_weather(response, start_date=start_date, end_date=end_date)
        return self._persist(tuple(_pending_weather_response(response) for response in responses))

    def ingest_open_meteo_dallas_historical(
        self,
        *,
        start_date: date,
        end_date: date,
    ) -> BronzeIngestionResult:
        """Backward-compatible Dallas convenience wrapper for historical weather."""

        return self.ingest_open_meteo_historical(
            city=TexasCity.DALLAS,
            start_date=start_date,
            end_date=end_date,
        )

    def ingest_open_meteo_dallas_forecast(self, *, forecast_hours: int = 48) -> BronzeIngestionResult:
        """Fetch Dallas forecast weather once and persist its validated capture."""

        response = self._weather_client.fetch_forecast(TexasCity.DALLAS, forecast_hours=forecast_hours)
        return self._persist((_pending_weather_response(response),))[0]

    def ingest_open_meteo_forecast(
        self, *, city: TexasCity, forecast_hours: int = 48
    ) -> BronzeIngestionResult:
        """Persist one city forecast with retrieval-time provenance from Open-Meteo."""

        response = self._weather_client.fetch_forecast(city, forecast_hours=forecast_hours)
        return self._persist((_pending_weather_response(response),))[0]

    def ingest_open_meteo_issued_ifs(
        self, *, city: TexasCity, model_run_utc: datetime, forecast_hours: int = 48
    ) -> BronzeIngestionResult:
        """Persist a run-identified IFS response without relabelling retrieval time as issuance."""

        response = self._weather_client.fetch_issued_ifs(
            city, model_run_utc=model_run_utc, forecast_hours=forecast_hours
        )
        return self._persist((_pending_weather_response(response),))[0]

    def _persist(self, pending_writes: tuple[_PendingBronzeWrite, ...]) -> tuple[BronzeIngestionResult, ...]:
        results: list[BronzeIngestionResult] = []
        for pending in pending_writes:
            write_result = self._bronze_writer.write(
                pending.capture,
                source_time_range=pending.source_time_range,
                units=pending.units,
            )
            results.append(
                BronzeIngestionResult(
                    provider=pending.capture.source,
                    dataset=pending.capture.dataset,
                    object_key=write_result.key,
                    record_count=pending.record_count,
                    source_time_range=pending.source_time_range,
                    already_exists=write_result.already_exists,
                    page_offset=pending.page_offset,
                )
            )
        return tuple(results)


def _pending_eia_page(page: EiaDemandPage) -> _PendingBronzeWrite:
    if not page.records or not page.periods_utc:
        raise ContractError("EIA Bronze persistence requires at least one validated record per page.")
    if len(page.records) != len(page.periods_utc):
        raise ContractError("EIA page records and validated periods must have matching lengths.")
    return _PendingBronzeWrite(
        capture=page.capture,
        source_time_range=_time_range(page.periods_utc),
        units={"value": _single_eia_unit(page.records)},
        record_count=len(page.records),
        page_offset=page.offset,
    )


def _pending_weather_response(response: WeatherResponse) -> _PendingBronzeWrite:
    if not response.hourly_times_utc:
        raise ContractError("Open-Meteo Bronze persistence requires validated hourly timestamps.")
    if not response.hourly_units:
        raise ContractError("Open-Meteo Bronze persistence requires validated hourly units.")
    if set(response.fields) != set(response.hourly_units):
        raise ContractError("Open-Meteo validated hourly units must match the requested fields.")
    return _PendingBronzeWrite(
        capture=response.capture,
        source_time_range=_time_range(response.hourly_times_utc),
        units=dict(response.hourly_units),
        record_count=len(response.hourly_times_utc),
        page_offset=None,
    )


def _single_eia_unit(records: tuple[Mapping[str, object], ...]) -> str:
    units: set[str] = set()
    for record in records:
        value_unit = record.get("value-units")
        if not isinstance(value_unit, str) or not value_unit.strip():
            raise ContractError("EIA value-units must be a non-empty string for Bronze persistence.")
        units.add(value_unit.strip())
    if len(units) != 1:
        raise ContractError("EIA Bronze page records must use one authoritative value-units value.")
    return next(iter(units))


def _time_range(timestamps: tuple[datetime, ...]) -> BronzeSourceTimeRange:
    if not timestamps:
        raise ContractError("Bronze source time ranges require at least one validated timestamp.")
    return BronzeSourceTimeRange(start=timestamps[0], end=timestamps[-1])


def _require_complete_eia_query(
    query: EiaDemandQuery,
    pending_pages: tuple[_PendingBronzeWrite, ...],
) -> None:
    """Reject incomplete historical EIA windows before any Bronze write occurs."""

    if not pending_pages:
        raise ContractError("EIA historical backfill response was empty for a requested window.")
    expected = query.start
    for page in sorted(pending_pages, key=lambda pending: pending.source_time_range.start):
        source_range = page.source_time_range
        expected_count = int((source_range.end - source_range.start).total_seconds() / 3600) + 1
        if page.record_count != expected_count or source_range.start != expected:
            raise ContractError("EIA historical backfill response did not cover every requested UTC hour.")
        expected = source_range.end + timedelta(hours=1)
    if expected != query.end + timedelta(hours=1):
        raise ContractError("EIA historical backfill response did not cover every requested UTC hour.")


def _require_complete_historical_weather(
    response: WeatherResponse,
    *,
    start_date: date,
    end_date: date,
) -> None:
    timezone_info = response.hourly_times_utc[0].tzinfo
    expected_start = datetime.combine(start_date, datetime.min.time(), tzinfo=timezone_info)
    expected_end = datetime.combine(end_date, datetime.max.time(), tzinfo=timezone_info).replace(
        minute=0,
        second=0,
        microsecond=0,
    )
    timestamps = response.hourly_times_utc
    expected_count = int((expected_end - expected_start).total_seconds() / 3600) + 1
    if timestamps[0] != expected_start or timestamps[-1] != expected_end or len(timestamps) != expected_count:
        raise ContractError("Open-Meteo historical backfill response did not cover every requested UTC hour.")
    if any(current - previous != timedelta(hours=1) for previous, current in zip(timestamps, timestamps[1:])):
        raise ContractError("Open-Meteo historical backfill response contains missing or non-hourly timestamps.")


def _require_available_historical_weather(response: WeatherResponse) -> None:
    if response.missing_hourly_value_counts:
        missing = ", ".join(
            f"{field}={count}" for field, count in sorted(response.missing_hourly_value_counts.items())
        )
        raise SourceDataUnavailableError(
            f"Open-Meteo historical weather is not available for {response.city.slug}: {missing}."
        )
