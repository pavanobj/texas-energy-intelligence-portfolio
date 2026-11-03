"""Exceptions raised when an external source violates an ingestion contract."""

from __future__ import annotations


class IngestionError(Exception):
    """Base class for expected ingestion failures."""


class ContractError(IngestionError):
    """A source response does not satisfy the documented local contract."""


class EmptySourceResponseError(IngestionError):
    """A source returned no records when an explicit empty result was not allowed."""


class SourceDataUnavailableError(IngestionError):
    """A structurally valid source response has no usable values yet."""


class TransportError(IngestionError):
    """The HTTP transport could not complete a request."""

    def __init__(self, message: str, *, phase: str | None = None) -> None:
        self.phase = phase
        super().__init__(message)


class ResponseDecodeError(ContractError):
    """A successful HTTP response did not contain valid JSON."""


class HttpStatusError(IngestionError):
    """A non-success HTTP response remained after retry handling."""

    def __init__(self, status_code: int, message: str | None = None) -> None:
        self.status_code = status_code
        super().__init__(message or f"HTTP request failed with status {status_code}.")


class PaginationError(ContractError):
    """Pagination metadata or pages cannot be reconciled safely."""


class BronzePersistenceError(IngestionError):
    """A validated Bronze envelope could not be persisted safely."""


class BackfillError(IngestionError):
    """A bounded historical backfill could not complete safely."""


class BackfillExecutionError(BackfillError):
    """A backfill stopped after reporting its completed immutable work."""
