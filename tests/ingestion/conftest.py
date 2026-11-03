"""In-memory HTTP helpers shared by ingestion contract tests."""

from __future__ import annotations

from collections import deque
import json

from texas_energy.ingestion.http import HttpRequest, HttpResponse


class ScriptedTransport:
    """Return scripted responses and retain requests without opening sockets."""

    def __init__(self, responses: list[HttpResponse | Exception]) -> None:
        self._responses = deque(responses)
        self.requests: list[HttpRequest] = []

    def send(self, request: HttpRequest) -> HttpResponse:
        self.requests.append(request)
        if not self._responses:
            raise AssertionError("Unexpected HTTP request.")
        response = self._responses.popleft()
        if isinstance(response, Exception):
            raise response
        return response


def json_response(payload: object, *, status_code: int = 200, headers: dict[str, str] | None = None) -> HttpResponse:
    return HttpResponse(
        status_code=status_code,
        headers=headers or {"content-type": "application/json"},
        body=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
    )
