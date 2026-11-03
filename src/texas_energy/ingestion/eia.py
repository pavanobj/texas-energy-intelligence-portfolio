"""EIA APIv2 contract for ERCOT (ERCO) hourly actual demand."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone

from .errors import ContractError, PaginationError
from .http import RetryingJsonClient
from .raw import RawCapture, redact_sensitive, utc_now


EIA_REGION_DATA_ENDPOINT = "https://api.eia.gov/v2/electricity/rto/region-data/data/"
EIA_RESPONDENT = "ERCO"
EIA_DEMAND_TYPE = "D"
EIA_PAGE_SIZE = 5_000
EIA_REQUIRED_RECORD_FIELDS = (
    "period",
    "respondent",
    "type",
    "value",
    "value-units",
)


@dataclass(frozen=True)
class EiaDemandQuery:
    """An inclusive, UTC-aligned EIA demand request."""

    start: datetime
    end: datetime
    page_size: int = EIA_PAGE_SIZE

    def __post_init__(self) -> None:
        start = _as_utc_hour(self.start, "start")
        end = _as_utc_hour(self.end, "end")
        if end < start:
            raise ValueError("end must not precede start.")
        if not 1 <= self.page_size <= EIA_PAGE_SIZE:
            raise ValueError(f"page_size must be between 1 and {EIA_PAGE_SIZE}.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    def parameters(self, *, offset: int, api_key: str) -> dict[str, str]:
        if offset < 0:
            raise ValueError("offset cannot be negative.")
        return {
            "api_key": api_key,
            "frequency": "hourly",
            "data[0]": "value",
            "facets[respondent][]": EIA_RESPONDENT,
            "facets[type][]": EIA_DEMAND_TYPE,
            "start": _format_eia_hour(self.start),
            "end": _format_eia_hour(self.end),
            "sort[0][column]": "period",
            "sort[0][direction]": "asc",
            "offset": str(offset),
            "length": str(self.page_size),
        }


@dataclass(frozen=True)
class EiaDemandPage:
    """One validated EIA page and its immutable raw response envelope."""

    offset: int
    records: tuple[Mapping[str, object], ...]
    total_records: int | None
    capture: RawCapture
    periods_utc: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class EiaDemandFetch:
    """A deterministic complete result for one EIA demand query."""

    query: EiaDemandQuery
    pages: tuple[EiaDemandPage, ...]
    records: tuple[Mapping[str, object], ...]


class EiaDemandClient:
    """Fetch EIA ERCOT actual demand without changing source values or units."""

    def __init__(
        self,
        http: RetryingJsonClient,
        *,
        api_key: str,
        retrieved_at=utc_now,
    ) -> None:
        normalized_api_key = api_key.strip()
        if not normalized_api_key:
            raise ValueError("EIA API key must not be empty.")
        self._http = http
        self._api_key = normalized_api_key
        self._retrieved_at = retrieved_at

    def fetch(self, query: EiaDemandQuery) -> EiaDemandFetch:
        pages: list[EiaDemandPage] = []
        all_records: list[Mapping[str, object]] = []
        seen_periods: set[str] = set()
        offset = 0
        expected_total: int | None = None
        previous_period: datetime | None = None

        while True:
            parameters = query.parameters(offset=offset, api_key=self._api_key)
            json_response = self._http.get_json(
                EIA_REGION_DATA_ENDPOINT,
                parameters,
                # The transport's Linux hard deadline bounds DNS/TLS/read;
                # keep urllib's socket timeout lower as a second guard.
                timeout_seconds=20.0,
            )
            capture_parameters = redact_sensitive(parameters)
            records, total = _validate_eia_payload(json_response.payload)
            if expected_total is None:
                expected_total = total
            elif total is not None and total != expected_total:
                raise PaginationError("EIA total changed between pages.")

            capture = RawCapture(
                source="eia",
                dataset="electricity/rto/region-data",
                endpoint=EIA_REGION_DATA_ENDPOINT,
                request_parameters=capture_parameters,
                raw_payload=json_response.response.body,
                retrieved_at=self._retrieved_at(),
                status_code=json_response.response.status_code,
                response_headers=json_response.response.headers,
            )
            page_periods: list[datetime] = []
            for record in records:
                period = _parse_eia_period(_required_string(record, "period"))
                if previous_period is not None and period <= previous_period:
                    raise PaginationError("EIA records must be strictly ascending by period.")
                previous_period = period
                period_key = _format_eia_hour(period)
                if period_key in seen_periods:
                    raise PaginationError(f"Duplicate EIA period returned: {period_key}.")
                seen_periods.add(period_key)
                page_periods.append(period)
                all_records.append(record)

            pages.append(EiaDemandPage(offset, tuple(records), total, capture, tuple(page_periods)))

            received = len(records)
            if expected_total is not None:
                if offset + received > expected_total:
                    raise PaginationError("EIA page exceeded its reported total.")
                if offset + received == expected_total:
                    break
            if received < query.page_size:
                break
            if received == 0:
                break
            offset += query.page_size

        if expected_total is not None and len(all_records) != expected_total:
            raise PaginationError(
                f"EIA returned {len(all_records)} records but reported total {expected_total}."
            )
        return EiaDemandFetch(query, tuple(pages), tuple(all_records))


def _validate_eia_payload(payload: Mapping[str, object]) -> tuple[list[Mapping[str, object]], int | None]:
    response = payload.get("response")
    if not isinstance(response, Mapping):
        raise ContractError("EIA payload must contain an object at response.")
    raw_records = response.get("data")
    if not isinstance(raw_records, list):
        raise ContractError("EIA payload must contain a list at response.data.")

    records: list[Mapping[str, object]] = []
    for raw_record in raw_records:
        if not isinstance(raw_record, Mapping):
            raise ContractError("Each EIA data item must be an object.")
        missing = [field for field in EIA_REQUIRED_RECORD_FIELDS if field not in raw_record]
        if missing:
            raise ContractError(f"EIA record is missing required fields: {', '.join(missing)}.")
        if raw_record["respondent"] != EIA_RESPONDENT:
            raise ContractError("EIA record respondent did not match ERCO.")
        if raw_record["type"] != EIA_DEMAND_TYPE:
            raise ContractError("EIA record type did not match D.")
        _parse_eia_period(_required_string(raw_record, "period"))
        records.append(dict(raw_record))

    total_value = response.get("total")
    if total_value is None:
        return records, None
    try:
        total = int(str(total_value))
    except ValueError as error:
        raise ContractError("EIA response.total must be an integer when present.") from error
    if total < 0:
        raise ContractError("EIA response.total cannot be negative.")
    return records, total


def _as_utc_hour(value: datetime, name: str) -> datetime:
    if value.tzinfo is None:
        raise ValueError(f"{name} must be timezone-aware.")
    normalized = value.astimezone(timezone.utc)
    if normalized.minute or normalized.second or normalized.microsecond:
        raise ValueError(f"{name} must align exactly to an hourly UTC boundary.")
    return normalized


def _parse_eia_period(value: str) -> datetime:
    try:
        return datetime.strptime(value, "%Y-%m-%dT%H").replace(tzinfo=timezone.utc)
    except ValueError as error:
        raise ContractError("EIA period must use the UTC hourly format YYYY-MM-DDTHH.") from error


def _format_eia_hour(value: datetime) -> str:
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H")


def _required_string(record: Mapping[str, object], field: str) -> str:
    value = record[field]
    if not isinstance(value, str):
        raise ContractError(f"EIA record field {field} must be a string.")
    return value
