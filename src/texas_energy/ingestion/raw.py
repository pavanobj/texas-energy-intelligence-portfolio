"""Source-response envelopes that a future S3 writer can persist unchanged."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from hashlib import sha256
import json
from urllib.parse import urlsplit, urlunsplit

from .errors import ContractError


MANIFEST_SCHEMA_VERSION = "1.0"
_REDACTED = "[REDACTED]"
_SENSITIVE_FIELD_NAMES = frozenset(
    {
        "access_key",
        "access_key_id",
        "api_key",
        "apikey",
        "authorization",
        "client_secret",
        "cookie",
        "credential",
        "id_token",
        "password",
        "refresh_token",
        "secret",
        "session_token",
        "set_cookie",
        "signature",
        "token",
        "x_api_key",
    }
)


def utc_now() -> datetime:
    """Return an aware UTC timestamp; injectable timestamps keep tests deterministic."""

    return datetime.now(timezone.utc)


def canonical_json_bytes(value: object) -> bytes:
    """Encode stable UTF-8 JSON for hashing and manifest persistence."""

    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def redact_sensitive(value: object) -> object:
    """Recursively redact known credential-bearing fields before persistence."""

    if isinstance(value, Mapping):
        return {
            str(key): _REDACTED if _is_sensitive_key(str(key)) else redact_sensitive(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    if isinstance(value, tuple):
        return [redact_sensitive(item) for item in value]
    return value


def request_fingerprint(
    endpoint: str,
    parameters: Mapping[str, object],
    headers: Mapping[str, object] | None = None,
) -> str:
    """Hash a canonical, redacted request identity rather than credential values."""

    safe_request = {
        "endpoint": _safe_endpoint(endpoint),
        "parameters": redact_sensitive(parameters),
        "headers": redact_sensitive(headers or {}),
    }
    return sha256(canonical_json_bytes(safe_request)).hexdigest()


@dataclass(frozen=True)
class RawCapture:
    """An immutable source response and a safe manifest for later object storage."""

    source: str
    dataset: str
    endpoint: str
    request_parameters: Mapping[str, object]
    raw_payload: bytes
    retrieved_at: datetime
    status_code: int
    response_headers: Mapping[str, str]
    source_contract_version: str = "v1"

    def __post_init__(self) -> None:
        if self.retrieved_at.tzinfo is None:
            raise ContractError("retrieved_at must be timezone-aware.")
        if not self.source or not self.dataset or not self.endpoint:
            raise ContractError("source, dataset, and endpoint are required.")
        object.__setattr__(self, "endpoint", _safe_endpoint(self.endpoint))
        object.__setattr__(self, "request_parameters", redact_sensitive(self.request_parameters))
        object.__setattr__(self, "response_headers", redact_sensitive(self.response_headers))

    @property
    def payload_sha256(self) -> str:
        return sha256(self.raw_payload).hexdigest()

    @property
    def fingerprint(self) -> str:
        return request_fingerprint(self.endpoint, self.request_parameters)

    def manifest(self) -> dict[str, object]:
        """Return a serializable manifest that never includes source credentials."""

        return {
            "manifest_schema_version": MANIFEST_SCHEMA_VERSION,
            "source_contract_version": self.source_contract_version,
            "source": self.source,
            "dataset": self.dataset,
            "endpoint": self.endpoint,
            "request_fingerprint": self.fingerprint,
            "request_parameters": self.request_parameters,
            "retrieved_at_utc": self.retrieved_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
            "status_code": self.status_code,
            "response_content_type": _header_value(self.response_headers, "content-type"),
            "raw_payload_sha256": self.payload_sha256,
            "raw_payload_bytes": len(self.raw_payload),
        }

    def manifest_bytes(self) -> bytes:
        return canonical_json_bytes(self.manifest())


def _is_sensitive_key(key: str) -> bool:
    normalized = key.lower().replace("-", "_")
    return normalized in _SENSITIVE_FIELD_NAMES or normalized.endswith(
        ("_authorization", "_credential", "_key", "_secret", "_signature", "_token")
    )


def _safe_endpoint(endpoint: str) -> str:
    """Return a query-free endpoint suitable for safe metadata persistence."""

    parsed = urlsplit(endpoint)
    if parsed.query or parsed.fragment:
        raise ContractError("Capture endpoints must not contain query strings or fragments.")
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def _header_value(headers: Mapping[str, str], expected_name: str) -> str | None:
    return next((value for key, value in headers.items() if key.lower() == expected_name), None)
