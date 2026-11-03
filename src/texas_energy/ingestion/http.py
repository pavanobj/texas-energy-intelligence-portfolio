"""A small HTTP boundary with deterministic retries and injectable transports.

The production clients use only :mod:`urllib`. Tests supply an in-memory
transport, so no test needs network access or a real credential.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import json
import signal
import socket
import ssl
import threading
from typing import Protocol
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .errors import HttpStatusError, ResponseDecodeError, TransportError


@dataclass(frozen=True)
class HttpRequest:
    """A request before it has been encoded into a URL."""

    url: str
    parameters: Mapping[str, str]
    headers: Mapping[str, str]
    timeout_seconds: float


@dataclass(frozen=True)
class HttpResponse:
    """The source response, retaining original bytes for future Bronze storage."""

    status_code: int
    headers: Mapping[str, str]
    body: bytes


@dataclass(frozen=True)
class JsonResponse:
    """A parsed JSON response plus the exact bytes received from the source."""

    request: HttpRequest
    response: HttpResponse
    payload: Mapping[str, object]


class HttpTransport(Protocol):
    """Injectable boundary for synchronous HTTP requests."""

    def send(self, request: HttpRequest) -> HttpResponse:
        """Send *request* and return the response without interpreting it."""


class UrllibTransport:
    """Standard-library transport for a later, explicitly approved runner."""

    def __init__(
        self,
        *,
        max_response_bytes: int = 4 * 1024 * 1024,
        hard_deadline_seconds: float | None = None,
    ) -> None:
        if max_response_bytes < 1:
            raise ValueError("max_response_bytes must be at least one byte.")
        if hard_deadline_seconds is not None and hard_deadline_seconds <= 0:
            raise ValueError("hard_deadline_seconds must be positive when configured.")
        self._max_response_bytes = max_response_bytes
        self._hard_deadline_seconds = hard_deadline_seconds

    def send(self, request: HttpRequest) -> HttpResponse:
        query = urlencode(sorted(request.parameters.items()))
        url = f"{request.url}?{query}" if query else request.url
        native_request = Request(url, headers=dict(request.headers), method="GET")
        try:
            with _hard_deadline(self._hard_deadline_seconds):
                response = urlopen(native_request, timeout=request.timeout_seconds)
        except HTTPError as error:
            return self._read_http_error(error)
        except TimeoutError:
            raise TransportError(
                "HTTP transport exceeded its bounded open deadline.", phase="open"
            ) from None
        except URLError as error:
            raise TransportError(
                "HTTP transport failed before a response was received.", phase=_url_error_phase(error)
            ) from None
        except (ConnectionError, OSError):
            raise TransportError(
                "HTTP transport failed before a response was received.", phase="connection"
            ) from None

        try:
            with response:
                with _hard_deadline(self._hard_deadline_seconds):
                    return HttpResponse(
                        status_code=response.status,
                        headers=dict(response.headers.items()),
                        body=self._read_limited(response),
                    )
        except TimeoutError:
            raise TransportError(
                "HTTP transport exceeded its bounded response-body deadline.", phase="response_body"
            ) from None
        except (URLError, ConnectionError, OSError):
            raise TransportError(
                "HTTP transport failed while reading a response body.", phase="response_body"
            ) from None

    def _read_http_error(self, error: HTTPError) -> HttpResponse:
        # ``HTTPError`` is itself a readable response stream. Bound its body
        # read independently because the original open context has exited.
        try:
            with _hard_deadline(self._hard_deadline_seconds):
                return HttpResponse(
                    status_code=error.code,
                    headers=dict(error.headers.items()) if error.headers else {},
                    body=self._read_limited(error),
                )
        except TimeoutError:
            raise TransportError(
                "HTTP transport exceeded its bounded HTTP-error-body deadline.", phase="http_error_body"
            ) from None
        except (URLError, ConnectionError, OSError):
            raise TransportError(
                "HTTP transport failed while reading an HTTP error body.", phase="http_error_body"
            ) from None

    def _read_limited(self, response: object) -> bytes:
        read = getattr(response, "read")
        body = read(self._max_response_bytes + 1)
        if not isinstance(body, bytes):
            raise TransportError("HTTP transport returned a non-bytes response body.")
        if len(body) > self._max_response_bytes:
            raise TransportError("HTTP response exceeded the configured size limit.")
        return body


def _url_error_phase(error: URLError) -> str:
    """Classify only an error category; never retain endpoint or reason text."""

    reason = error.reason
    if isinstance(reason, socket.gaierror):
        return "dns"
    if isinstance(reason, ssl.SSLError):
        return "tls"
    if isinstance(reason, TimeoutError):
        return "open"
    return "connection"


class _hard_deadline:
    """Bound one blocking urllib operation on Linux main threads.

    ``urlopen(timeout=...)`` controls socket operations, but a hard deadline
    also bounds DNS, TLS, and response-body phases when the host supports
    ``SIGALRM``. Windows retains urllib's normal socket timeout behavior.
    """

    def __init__(self, seconds: float | None) -> None:
        self._seconds = seconds
        self._enabled = bool(
            seconds is not None
            and hasattr(signal, "SIGALRM")
            and threading.current_thread() is threading.main_thread()
        )
        self._previous_handler: object | None = None

    def __enter__(self) -> None:
        if not self._enabled:
            return
        self._previous_handler = signal.signal(signal.SIGALRM, self._raise_timeout)
        signal.setitimer(signal.ITIMER_REAL, self._seconds)

    def __exit__(self, exception_type: object, exception: object, traceback: object) -> None:
        if self._enabled:
            signal.setitimer(signal.ITIMER_REAL, 0)
            assert self._previous_handler is not None
            signal.signal(signal.SIGALRM, self._previous_handler)

    @staticmethod
    def _raise_timeout(signum: int, frame: object) -> None:
        raise TimeoutError("HTTP request exceeded its hard deadline.")


@dataclass(frozen=True)
class RetryPolicy:
    """Retry only transient failures; callers inject sleeping in tests."""

    max_attempts: int = 3
    base_delay_seconds: float = 0.5
    max_delay_seconds: float = 10.0
    max_retry_after_seconds: float | None = None

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least one.")
        if self.base_delay_seconds < 0 or self.max_delay_seconds < 0:
            raise ValueError("retry delays cannot be negative.")
        if self.max_retry_after_seconds is not None and self.max_retry_after_seconds < 0:
            raise ValueError("max_retry_after_seconds cannot be negative.")


class RetryingJsonClient:
    """Fetch JSON without logging URLs, query parameters, or response bodies."""

    def __init__(
        self,
        transport: HttpTransport,
        *,
        retry_policy: RetryPolicy | None = None,
        sleep: Callable[[float], None] | None = None,
        now: Callable[[], datetime] | None = None,
        minimum_interval_seconds_by_host: Mapping[str, float] | None = None,
        monotonic: Callable[[], float] | None = None,
    ) -> None:
        self._transport = transport
        self._retry_policy = retry_policy or RetryPolicy()
        self._sleep = sleep or __import__("time").sleep
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._monotonic = monotonic or __import__("time").monotonic
        self._minimum_interval_seconds_by_host = _validate_minimum_intervals(minimum_interval_seconds_by_host or {})
        self._last_request_started_at_by_host: dict[str, float] = {}

    def get_json(
        self,
        url: str,
        parameters: Mapping[str, str],
        *,
        headers: Mapping[str, str] | None = None,
        timeout_seconds: float = 30.0,
    ) -> JsonResponse:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive.")
        request = HttpRequest(
            url=url,
            parameters=dict(parameters),
            headers=dict(headers or {}),
            timeout_seconds=timeout_seconds,
        )
        last_status: int | None = None

        for attempt in range(1, self._retry_policy.max_attempts + 1):
            self._wait_for_host_rate_limit(request.url)
            try:
                response = self._transport.send(request)
            except TransportError:
                if attempt == self._retry_policy.max_attempts:
                    raise
                self._sleep(self._retry_delay(attempt, None))
                continue

            last_status = response.status_code
            if 200 <= response.status_code < 300:
                return JsonResponse(
                    request=request,
                    response=response,
                    payload=_decode_json_object(response.body),
                )

            if response.status_code == 429 or 500 <= response.status_code < 600:
                if attempt < self._retry_policy.max_attempts:
                    self._sleep(self._retry_delay(attempt, response.headers))
                    continue
            raise HttpStatusError(response.status_code)

        raise HttpStatusError(last_status or 0)

    def _retry_delay(self, attempt: int, headers: Mapping[str, str] | None) -> float:
        retry_after = _retry_after_seconds(headers or {}, now=self._now())
        if retry_after is not None:
            budget = self._retry_policy.max_retry_after_seconds
            if budget is not None and retry_after > budget:
                raise TransportError("Server Retry-After delay exceeds the configured execution budget.")
            return retry_after
        return min(
            self._retry_policy.base_delay_seconds * (2 ** (attempt - 1)),
            self._retry_policy.max_delay_seconds,
        )

    def _wait_for_host_rate_limit(self, url: str) -> None:
        """Apply a lower-bound interval to every source request and retry."""

        host = urlsplit(url).netloc.lower()
        interval = self._minimum_interval_seconds_by_host.get(host)
        if interval is None:
            return
        now = self._monotonic()
        previous = self._last_request_started_at_by_host.get(host)
        if previous is not None:
            delay = interval - (now - previous)
            if delay > 0:
                self._sleep(delay)
        self._last_request_started_at_by_host[host] = self._monotonic()


def _decode_json_object(body: bytes) -> Mapping[str, object]:
    try:
        decoded = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        raise ResponseDecodeError("Source response was not valid UTF-8 JSON.") from None
    if not isinstance(decoded, dict):
        raise ResponseDecodeError("Source JSON response must be an object.")
    return decoded


def _validate_minimum_intervals(values: Mapping[str, float]) -> dict[str, float]:
    normalized: dict[str, float] = {}
    for host, interval in values.items():
        if not isinstance(host, str) or not host.strip():
            raise ValueError("Rate-limit hosts must be non-empty strings.")
        if isinstance(interval, bool) or not isinstance(interval, (int, float)) or interval <= 0:
            raise ValueError("Rate-limit intervals must be positive seconds.")
        normalized[host.lower()] = float(interval)
    return normalized


def _retry_after_seconds(headers: Mapping[str, str], *, now: datetime | None = None) -> float | None:
    """Return a non-negative seconds value for an HTTP ``Retry-After`` header."""

    raw_value = next((value for key, value in headers.items() if key.lower() == "retry-after"), None)
    if raw_value is None:
        return None
    try:
        return max(0.0, float(raw_value))
    except ValueError:
        try:
            retry_at = parsedate_to_datetime(raw_value)
        except (TypeError, ValueError, IndexError):
            return None
        if retry_at.tzinfo is None:
            return None
        return max(0.0, (retry_at.astimezone(timezone.utc) - (now or datetime.now(timezone.utc))).total_seconds())
