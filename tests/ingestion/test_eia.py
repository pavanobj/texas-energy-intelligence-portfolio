from __future__ import annotations

from datetime import datetime, timezone

import pytest

from texas_energy.ingestion.eia import EiaDemandClient, EiaDemandQuery
from texas_energy.ingestion.errors import ContractError, PaginationError
from texas_energy.ingestion.http import RetryingJsonClient

from .conftest import ScriptedTransport, json_response


def _record(hour: int) -> dict[str, object]:
    return {
        "period": f"2024-01-01T{hour:02d}",
        "respondent": "ERCO",
        "respondent-name": "Electric Reliability Council of Texas, Inc.",
        "type": "D",
        "type-name": "Demand",
        "value": "45000",
        "value-units": "megawatthours",
    }


def _query(*, page_size: int = 2) -> EiaDemandQuery:
    return EiaDemandQuery(
        start=datetime(2024, 1, 1, 0, tzinfo=timezone.utc),
        end=datetime(2024, 1, 1, 2, tzinfo=timezone.utc),
        page_size=page_size,
    )


def test_eia_paginates_deterministically_and_preserves_raw_fields() -> None:
    transport = ScriptedTransport(
        [
            json_response({"response": {"total": "3", "data": [_record(0), _record(1)]}}),
            json_response({"response": {"total": "3", "data": [_record(2)]}}),
        ]
    )
    client = EiaDemandClient(
        RetryingJsonClient(transport),
        api_key="not-a-real-key",
        retrieved_at=lambda: datetime(2024, 1, 1, tzinfo=timezone.utc),
    )

    result = client.fetch(_query())

    assert [request.parameters["offset"] for request in transport.requests] == ["0", "2"]
    assert transport.requests[0].parameters["facets[respondent][]"] == "ERCO"
    assert transport.requests[0].parameters["facets[type][]"] == "D"
    assert transport.requests[0].parameters["frequency"] == "hourly"
    assert len(result.records) == 3
    assert result.records[0]["value"] == "45000"
    assert result.records[0]["value-units"] == "megawatthours"
    assert "not-a-real-key" not in str(result.pages[0].capture.manifest())


def test_eia_rejects_missing_authoritative_unit_field() -> None:
    invalid_record = _record(0)
    invalid_record.pop("value-units")
    client = EiaDemandClient(
        RetryingJsonClient(ScriptedTransport([json_response({"response": {"data": [invalid_record]}})])),
        api_key="not-a-real-key",
    )

    with pytest.raises(ContractError, match="value-units"):
        client.fetch(_query())


def test_eia_rejects_duplicate_or_unsorted_periods() -> None:
    transport = ScriptedTransport(
        [json_response({"response": {"total": "2", "data": [_record(1), _record(0)]}})]
    )
    client = EiaDemandClient(RetryingJsonClient(transport), api_key="not-a-real-key")

    with pytest.raises(PaginationError, match="strictly ascending"):
        client.fetch(_query())


def test_eia_terminates_on_a_short_page_when_total_is_missing() -> None:
    transport = ScriptedTransport([json_response({"response": {"data": [_record(0)]}})])
    result = EiaDemandClient(RetryingJsonClient(transport), api_key="test-key").fetch(_query())

    assert len(result.records) == 1
    assert [request.parameters["offset"] for request in transport.requests] == ["0"]


def test_eia_requests_one_empty_page_for_an_unknown_exact_page_multiple() -> None:
    transport = ScriptedTransport(
        [
            json_response({"response": {"data": [_record(0), _record(1)]}}),
            json_response({"response": {"data": []}}),
        ]
    )
    result = EiaDemandClient(RetryingJsonClient(transport), api_key="test-key").fetch(_query())

    assert len(result.records) == 2
    assert [request.parameters["offset"] for request in transport.requests] == ["0", "2"]


def test_eia_rejects_inconsistent_totals_between_pages() -> None:
    transport = ScriptedTransport(
        [
            json_response({"response": {"total": "3", "data": [_record(0), _record(1)]}}),
            json_response({"response": {"total": "4", "data": [_record(2)]}}),
        ]
    )
    client = EiaDemandClient(RetryingJsonClient(transport), api_key="test-key")

    with pytest.raises(PaginationError, match="total changed"):
        client.fetch(_query())


def test_eia_rejects_duplicate_periods_across_page_boundaries() -> None:
    transport = ScriptedTransport(
        [
            json_response({"response": {"total": "3", "data": [_record(0), _record(1)]}}),
            json_response({"response": {"total": "3", "data": [_record(1)]}}),
        ]
    )
    client = EiaDemandClient(RetryingJsonClient(transport), api_key="test-key")

    with pytest.raises(PaginationError, match="strictly ascending"):
        client.fetch(_query())


def test_eia_query_requires_timezone_aware_hour_boundaries() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        EiaDemandQuery(datetime(2024, 1, 1), datetime(2024, 1, 1, tzinfo=timezone.utc))

    with pytest.raises(ValueError, match="hourly UTC boundary"):
        EiaDemandQuery(
            datetime(2024, 1, 1, 0, 30, tzinfo=timezone.utc),
            datetime(2024, 1, 1, 1, tzinfo=timezone.utc),
        )


def test_eia_client_normalizes_surrounding_api_key_whitespace_before_request() -> None:
    transport = ScriptedTransport([json_response({"response": {"data": []}})])
    client = EiaDemandClient(RetryingJsonClient(transport), api_key="  test-key\n")

    client.fetch(_query())

    assert transport.requests[0].parameters["api_key"] == "test-key"
