"""Safe, deterministic S3 Bronze persistence for validated source captures."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import ipaddress
import json
import re
from typing import Protocol
from urllib.parse import parse_qsl, quote, urlsplit

from .errors import BronzePersistenceError
from .raw import RawCapture, canonical_json_bytes, redact_sensitive


BRONZE_ENVELOPE_SCHEMA_VERSION = "1.1"
DEFAULT_BRONZE_PREFIX = "bronze"
DEFAULT_MAX_OBJECT_BYTES = 5 * 1024 * 1024
DEFAULT_CONDITIONAL_CONFLICT_ATTEMPTS = 2
_MAX_S3_KEY_BYTES = 1_024
_MAX_KEY_IDENTITY_CHARS = 256
_BUCKET_PATTERN = re.compile(r"^[a-z0-9][a-z0-9.-]{1,61}[a-z0-9]$")
_PARTITION_VALUE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,127}$")
_SENSITIVE_QUERY_NAMES = frozenset(
    {
        "api_key",
        "apikey",
        "authorization",
        "access_token",
        "client_secret",
        "credential",
        "password",
        "secret",
        "signature",
        "token",
        "x_api_key",
    }
)


class S3PutObjectClient(Protocol):
    """The boto3 S3 client operation required by the Bronze writer."""

    def put_object(self, **kwargs: object) -> Mapping[str, object]:
        """Write one object to S3."""


@dataclass(frozen=True)
class BronzeSourceTimeRange:
    """The UTC source-data interval represented by one raw capture."""

    start: datetime
    end: datetime

    def __post_init__(self) -> None:
        if self.start.tzinfo is None or self.end.tzinfo is None:
            raise ValueError("Bronze source time range timestamps must be timezone-aware.")
        start = self.start.astimezone(timezone.utc)
        end = self.end.astimezone(timezone.utc)
        if end < start:
            raise ValueError("Bronze source time range end must not precede start.")
        object.__setattr__(self, "start", start)
        object.__setattr__(self, "end", end)

    @property
    def partition_date(self) -> str:
        """Return the UTC date of the first source record for Hive-style partitioning."""

        return self.start.date().isoformat()


@dataclass(frozen=True)
class BronzeWriteResult:
    """Safe S3 write facts that a future Lambda handler can log or return."""

    bucket: str
    key: str
    envelope_bytes: int
    already_exists: bool


class BronzeS3Writer:
    """Persist generic sanitized captures using an injected boto3-compatible S3 client."""

    def __init__(
        self,
        s3_client: S3PutObjectClient,
        *,
        bucket_name: str,
        prefix: str = DEFAULT_BRONZE_PREFIX,
        max_object_bytes: int = DEFAULT_MAX_OBJECT_BYTES,
        conditional_conflict_attempts: int = DEFAULT_CONDITIONAL_CONFLICT_ATTEMPTS,
    ) -> None:
        _validate_bucket_name(bucket_name)
        _validate_prefix(prefix)
        if isinstance(max_object_bytes, bool) or not isinstance(max_object_bytes, int) or max_object_bytes < 1:
            raise ValueError("max_object_bytes must be a positive integer.")
        if (
            isinstance(conditional_conflict_attempts, bool)
            or not isinstance(conditional_conflict_attempts, int)
            or conditional_conflict_attempts < 1
        ):
            raise ValueError("conditional_conflict_attempts must be a positive integer.")
        self._s3_client = s3_client
        self._bucket_name = bucket_name
        self._prefix = prefix
        self._max_object_bytes = max_object_bytes
        self._conditional_conflict_attempts = conditional_conflict_attempts

    def object_key(self, capture: RawCapture, source_time_range: BronzeSourceTimeRange) -> str:
        """Build the deterministic, safe S3 key for a logical Bronze capture."""

        source = _encoded_key_identity("source", capture.source)
        dataset = _encoded_key_identity("dataset", capture.dataset)
        key = (
            f"{self._prefix}/v{BRONZE_ENVELOPE_SCHEMA_VERSION.split('.', maxsplit=1)[0]}"
            f"/source={source}"
            f"/dataset={dataset}"
            f"/data_date={source_time_range.partition_date}"
            f"/request={capture.fingerprint}"
            f"/raw_payload={capture.payload_sha256}.json"
        )
        _validate_object_key(key)
        return key

    def write(
        self,
        capture: RawCapture,
        *,
        source_time_range: BronzeSourceTimeRange,
        units: Mapping[str, str] | None = None,
    ) -> BronzeWriteResult:
        """Serialize one sanitized source capture and write it with SSE-S3 encryption."""

        key = self.object_key(capture, source_time_range)
        _validate_capture_size(capture, max_object_bytes=self._max_object_bytes)
        envelope = bronze_envelope(capture, source_time_range=source_time_range, units=units)
        body = _serialize_envelope(envelope, self._max_object_bytes)
        for attempt in range(1, self._conditional_conflict_attempts + 1):
            try:
                self._s3_client.put_object(
                    Bucket=self._bucket_name,
                    Key=key,
                    Body=body,
                    ContentType="application/json",
                    ServerSideEncryption="AES256",
                    IfNoneMatch="*",
                )
            except Exception as error:
                if _is_precondition_failed(error):
                    return BronzeWriteResult(
                        bucket=self._bucket_name,
                        key=key,
                        envelope_bytes=len(body),
                        already_exists=True,
                    )
                if _is_conditional_request_conflict(error) and attempt < self._conditional_conflict_attempts:
                    continue
                if _is_conditional_request_conflict(error):
                    raise BronzePersistenceError(
                        "S3 Bronze conditional write conflicted after the configured retry limit."
                    ) from None
                raise BronzePersistenceError(
                    f"S3 Bronze write failed for bucket {self._bucket_name!r} and validated object key {key!r}."
                ) from None
            return BronzeWriteResult(
                bucket=self._bucket_name,
                key=key,
                envelope_bytes=len(body),
                already_exists=False,
            )
        raise AssertionError("Conditional S3 write loop terminated unexpectedly.")


def bronze_envelope(
    capture: RawCapture,
    *,
    source_time_range: BronzeSourceTimeRange,
    units: Mapping[str, str] | None = None,
) -> dict[str, object]:
    """Create the versioned, source-preserving envelope stored in one Bronze object."""

    _validate_capture_size(capture)
    sanitized_payload = _sanitized_source_payload(capture.raw_payload)
    sanitized_payload_bytes = _canonical_sanitized_payload_bytes(sanitized_payload)
    safe_units = _validated_units(units)
    return {
        "schema_version": BRONZE_ENVELOPE_SCHEMA_VERSION,
        "source": capture.source,
        "dataset": capture.dataset,
        "ingested_at_utc": _utc_text(capture.retrieved_at),
        "source_time_range_utc": {
            "start": _utc_text(source_time_range.start),
            "end": _utc_text(source_time_range.end),
        },
        "request_fingerprint": capture.fingerprint,
        "sanitized_payload_sha256": sha256(sanitized_payload_bytes).hexdigest(),
        "units": safe_units,
        "capture": capture.manifest(),
        "source_payload": sanitized_payload,
    }


def _serialize_envelope(envelope: Mapping[str, object], max_object_bytes: int) -> bytes:
    try:
        body = canonical_json_bytes(envelope)
    except (TypeError, ValueError):
        raise BronzePersistenceError("Bronze envelope could not be serialized as JSON.") from None
    if len(body) > max_object_bytes:
        raise BronzePersistenceError("Bronze envelope exceeds the configured object-size limit.")
    return body


def _canonical_sanitized_payload_bytes(sanitized_payload: object) -> bytes:
    try:
        return canonical_json_bytes(sanitized_payload)
    except (TypeError, ValueError):
        raise BronzePersistenceError("Sanitized Bronze source payload could not be serialized as JSON.") from None


def _validate_capture_size(capture: RawCapture, *, max_object_bytes: int | None = None) -> None:
    if not capture.raw_payload:
        raise BronzePersistenceError("Bronze capture payload must not be empty.")
    if max_object_bytes is not None and len(capture.raw_payload) > max_object_bytes:
        raise BronzePersistenceError("Bronze capture payload exceeds the configured object-size limit.")


def _sanitized_source_payload(raw_payload: bytes) -> object:
    try:
        decoded = json.loads(raw_payload.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise BronzePersistenceError("Bronze capture payload must be valid UTF-8 JSON.") from None
    return _sanitize_payload_value(decoded)


def _sanitize_payload_value(value: object, field_name: str | None = None) -> object:
    if isinstance(value, Mapping):
        redacted = redact_sensitive(value)
        return {
            str(key): _sanitize_payload_value(item, field_name=str(key))
            for key, item in redacted.items()
        }
    if isinstance(value, list):
        return [_sanitize_payload_value(item) for item in value]
    if isinstance(value, str) and _contains_sensitive_url_or_query(value, field_name):
        return "[REDACTED]"
    return value


def _validated_units(units: Mapping[str, str] | None) -> dict[str, str] | None:
    if units is None:
        return None
    if not isinstance(units, Mapping):
        raise ValueError("Bronze units must be a mapping when provided.")
    validated: dict[str, str] = {}
    for field, unit in units.items():
        if not isinstance(field, str) or not _PARTITION_VALUE_PATTERN.fullmatch(field):
            raise ValueError("Bronze unit field names must use safe identifier characters.")
        if not isinstance(unit, str) or not unit.strip():
            raise ValueError("Bronze unit values must be non-empty strings.")
        if redact_sensitive({field: unit})[field] != unit:
            raise ValueError("Bronze unit field names must not be credential-bearing identifiers.")
        validated[field] = unit.strip()
    return dict(sorted(validated.items()))


def _validate_bucket_name(bucket_name: str) -> None:
    if not isinstance(bucket_name, str) or not _BUCKET_PATTERN.fullmatch(bucket_name):
        raise ValueError("S3 bucket names must be 3-63 lowercase characters using letters, numbers, dots, and hyphens.")
    if ".." in bucket_name:
        raise ValueError("S3 bucket names must not contain adjacent dots.")
    try:
        ipaddress.ip_address(bucket_name)
    except ValueError:
        return
    raise ValueError("S3 bucket names must not use an IPv4 address format.")


def _validate_prefix(prefix: str) -> None:
    if not isinstance(prefix, str) or not prefix or prefix.startswith("/") or prefix.endswith("/"):
        raise ValueError("Bronze S3 prefixes must be non-empty relative paths without leading or trailing slashes.")
    for segment in prefix.split("/"):
        _validate_partition_value("prefix segment", segment)


def _validate_partition_value(name: str, value: str) -> None:
    if not isinstance(value, str) or not _PARTITION_VALUE_PATTERN.fullmatch(value):
        raise ValueError(f"Bronze {name} must use safe identifier characters.")


def _encoded_key_identity(name: str, value: str) -> str:
    """Return a path-segment-safe, reversible representation of capture metadata."""

    if not isinstance(value, str) or not value or len(value) > _MAX_KEY_IDENTITY_CHARS:
        raise ValueError(f"Bronze {name} must be a non-empty identifier no longer than 256 characters.")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f"Bronze {name} must not contain control characters.")
    return quote(value, safe="-_.")


def _validate_object_key(key: str) -> None:
    if not key or key.startswith("/") or len(key.encode("utf-8")) > _MAX_S3_KEY_BYTES:
        raise ValueError("Bronze S3 object keys must be relative and no more than 1,024 UTF-8 bytes.")


def _is_url_field(field_name: str | None) -> bool:
    if field_name is None:
        return False
    normalized = field_name.lower().replace("-", "_")
    return normalized in {"url", "uri", "endpoint", "href", "link"} or normalized.endswith(("_url", "_uri"))


def _contains_sensitive_url_or_query(value: str, field_name: str | None) -> bool:
    parsed = urlsplit(value)
    is_url = parsed.scheme in {"http", "https"} or bool(parsed.netloc)
    normalized_field_name = (field_name or "").lower().replace("-", "_")
    query = parsed.query if is_url else value if normalized_field_name in {"query", "query_string"} else ""
    if is_url and parsed.fragment:
        return True
    return any(_is_sensitive_query_name(name) for name, _ in parse_qsl(query, keep_blank_values=True))


def _is_sensitive_query_name(name: str) -> bool:
    normalized = name.lower().replace("-", "_")
    return normalized in _SENSITIVE_QUERY_NAMES or normalized.endswith(
        ("_authorization", "_credential", "_key", "_secret", "_signature", "_token")
    )


def _is_precondition_failed(error: Exception) -> bool:
    error_code, http_status = _s3_error_details(error)
    return error_code == "PreconditionFailed" or http_status == 412


def _is_conditional_request_conflict(error: Exception) -> bool:
    error_code, http_status = _s3_error_details(error)
    return error_code == "ConditionalRequestConflict" or http_status == 409


def _s3_error_details(error: Exception) -> tuple[str | None, int | None]:
    """Extract only structured S3 error facts; never retain provider error text."""

    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None, None
    error_section = response.get("Error")
    response_metadata = response.get("ResponseMetadata")
    error_code = error_section.get("Code") if isinstance(error_section, Mapping) else None
    http_status = response_metadata.get("HTTPStatusCode") if isinstance(response_metadata, Mapping) else None
    if not isinstance(error_code, str):
        error_code = None
    if isinstance(http_status, bool) or not isinstance(http_status, int):
        http_status = None
    return error_code, http_status


def _utc_text(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
