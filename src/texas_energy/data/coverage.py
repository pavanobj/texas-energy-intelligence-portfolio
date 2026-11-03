"""Local, read-only coverage checks for validated historical Bronze envelopes.

The audit deliberately operates before Silver deduplication. It therefore
surfaces missing city-hours, duplicate captures, and source revisions instead
of allowing deterministic Silver selection to hide them. It does not interpolate
or fabricate observations.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta, timezone
from typing import Iterable

from .silver import (
    BronzeEnvelopeInput,
    DemandSilverRecord,
    WeatherSilverRecord,
    validated_bronze_records,
)


@dataclass(frozen=True, order=True)
class HistoricalBusinessKey:
    """One demand or historical-weather hourly identity safe to report to operators."""

    dataset: str
    period_utc: datetime
    city: str | None = None


@dataclass(frozen=True, order=True)
class SourceRevision:
    """Distinct source-provenance versions observed for one business key."""

    key: HistoricalBusinessKey
    provenance_versions: int


@dataclass(frozen=True)
class HistoricalCoverageAudit:
    """Coverage facts for a requested UTC date range, before Silver writes."""

    start_date: date
    end_date: date
    expected_demand_hours: int
    expected_weather_city_hours: int
    observed_demand_records: int
    observed_historical_weather_records: int
    missing_demand_hours: tuple[datetime, ...]
    missing_weather_city_hours: tuple[HistoricalBusinessKey, ...]
    duplicate_demand_hours: tuple[HistoricalBusinessKey, ...]
    duplicate_weather_city_hours: tuple[HistoricalBusinessKey, ...]
    source_revisions: tuple[SourceRevision, ...]
    unexpected_demand_hours: tuple[datetime, ...]
    unexpected_weather_city_hours: tuple[HistoricalBusinessKey, ...]

    @property
    def is_complete_without_revisions(self) -> bool:
        """True only when there are no gaps, duplicates, out-of-range rows, or revisions."""

        return not (
            self.missing_demand_hours
            or self.missing_weather_city_hours
            or self.duplicate_demand_hours
            or self.duplicate_weather_city_hours
            or self.source_revisions
            or self.unexpected_demand_hours
            or self.unexpected_weather_city_hours
        )

    @property
    def is_coverage_complete(self) -> bool:
        """True when source revisions are the only observed exception."""

        return not (
            self.missing_demand_hours
            or self.missing_weather_city_hours
            or self.duplicate_demand_hours
            or self.duplicate_weather_city_hours
            or self.unexpected_demand_hours
            or self.unexpected_weather_city_hours
        )


def audit_historical_bronze(
    envelopes: Iterable[BronzeEnvelopeInput],
    *,
    start_date: date,
    end_date: date,
) -> HistoricalCoverageAudit:
    """Validate Bronze inputs and report historical coverage before any Delta write.

    Invalid schemas, unexpected units, malformed periods, and unknown cities are
    rejected by ``validated_bronze_records``. A valid-but-incomplete range is
    returned as structured facts so a caller can fail the batch or quarantine it.
    """

    if end_date < start_date:
        raise ValueError("end_date must not precede start_date.")
    records = validated_bronze_records(envelopes)
    expected_hours = tuple(_utc_hours(start_date, end_date))
    expected_hour_set = set(expected_hours)
    expected_weather = {
        HistoricalBusinessKey("historical-weather", period, city)
        for city in _CANONICAL_CITY_SLUGS
        for period in expected_hours
    }

    demand_by_hour: dict[datetime, list[DemandSilverRecord]] = defaultdict(list)
    for record in records.demand:
        demand_by_hour[record.period_utc].append(record)
    weather_by_key: dict[HistoricalBusinessKey, list[WeatherSilverRecord]] = defaultdict(list)
    for record in records.historical_weather:
        weather_by_key[HistoricalBusinessKey("historical-weather", record.observed_at_utc, record.city)].append(record)

    missing_demand = tuple(sorted(expected_hour_set.difference(demand_by_hour)))
    observed_weather_keys = set(weather_by_key)
    missing_weather = tuple(sorted(expected_weather.difference(observed_weather_keys)))
    duplicate_demand = tuple(
        HistoricalBusinessKey("electricity/rto/region-data", period)
        for period, candidates in sorted(demand_by_hour.items())
        if len(candidates) > 1
    )
    duplicate_weather = tuple(key for key, candidates in sorted(weather_by_key.items()) if len(candidates) > 1)
    revisions = tuple(
        sorted(
            [
                SourceRevision(HistoricalBusinessKey("electricity/rto/region-data", period), len(_provenance_versions(candidates)))
                for period, candidates in demand_by_hour.items()
                if len(_provenance_versions(candidates)) > 1
            ]
            + [
                SourceRevision(key, len(_provenance_versions(candidates)))
                for key, candidates in weather_by_key.items()
                if len(_provenance_versions(candidates)) > 1
            ]
        )
    )
    return HistoricalCoverageAudit(
        start_date=start_date,
        end_date=end_date,
        expected_demand_hours=len(expected_hours),
        expected_weather_city_hours=len(expected_weather),
        observed_demand_records=sum(len(candidates) for candidates in demand_by_hour.values()),
        observed_historical_weather_records=sum(len(candidates) for candidates in weather_by_key.values()),
        missing_demand_hours=missing_demand,
        missing_weather_city_hours=missing_weather,
        duplicate_demand_hours=duplicate_demand,
        duplicate_weather_city_hours=duplicate_weather,
        source_revisions=revisions,
        unexpected_demand_hours=tuple(sorted(set(demand_by_hour).difference(expected_hour_set))),
        unexpected_weather_city_hours=tuple(sorted(observed_weather_keys.difference(expected_weather))),
    )


_CANONICAL_CITY_SLUGS = ("austin", "dallas", "el_paso", "houston", "san_antonio")


def _utc_hours(start_date: date, end_date: date) -> Iterable[datetime]:
    current = datetime.combine(start_date, time.min, tzinfo=timezone.utc)
    end = datetime.combine(end_date, time(hour=23), tzinfo=timezone.utc)
    while current <= end:
        yield current
        current += timedelta(hours=1)


def _provenance_versions(records: Iterable[DemandSilverRecord | WeatherSilverRecord]) -> set[tuple[str, str, str]]:
    return {
        (record.source_request_fingerprint, record.source_raw_payload_sha256, record.sanitized_payload_sha256)
        for record in records
    }
