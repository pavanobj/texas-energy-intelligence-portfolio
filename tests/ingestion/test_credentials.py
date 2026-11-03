"""Mock-only tests for EIA API-key retrieval from SSM Parameter Store."""

from __future__ import annotations

import pytest

from texas_energy.ingestion.credentials import (
    EIA_API_KEY_PARAMETER,
    EiaCredentialAccessDenied,
    EiaCredentialEmpty,
    EiaCredentialNotFound,
    EiaCredentialUnavailable,
    create_ssm_parameter_client,
    get_eia_api_key_from_ssm,
)


class FakeSsmClient:
    def __init__(self, response: object) -> None:
        self._response = response
        self.calls: list[dict[str, object]] = []

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> dict[str, object]:
        self.calls.append({"Name": Name, "WithDecryption": WithDecryption})
        if isinstance(self._response, Exception):
            raise self._response
        assert isinstance(self._response, dict)
        return self._response


class FakeAwsError(Exception):
    def __init__(self, code: str) -> None:
        self.response = {"Error": {"Code": code}}


class NoCredentialsError(Exception):
    pass


def test_retrieves_stripped_eia_key_with_ssm_decryption() -> None:
    client = FakeSsmClient({"Parameter": {"Value": " test-key "}})

    assert get_eia_api_key_from_ssm(client=client) == "test-key"
    assert client.calls == [{"Name": EIA_API_KEY_PARAMETER, "WithDecryption": True}]


@pytest.mark.parametrize(
    ("error", "expected_exception"),
    [
        (FakeAwsError("AccessDeniedException"), EiaCredentialAccessDenied),
        (FakeAwsError("ParameterNotFound"), EiaCredentialNotFound),
        (NoCredentialsError(), EiaCredentialUnavailable),
    ],
)
def test_maps_ssm_failures_to_safe_credential_errors(error: Exception, expected_exception: type[Exception]) -> None:
    with pytest.raises(expected_exception):
        get_eia_api_key_from_ssm(client=FakeSsmClient(error))


@pytest.mark.parametrize("response", [{}, {"Parameter": {}}, {"Parameter": {"Value": "  "}}])
def test_rejects_missing_or_empty_ssm_parameter_values(response: dict[str, object]) -> None:
    with pytest.raises(EiaCredentialEmpty):
        get_eia_api_key_from_ssm(client=FakeSsmClient(response))


def test_client_factory_uses_named_profile_or_default_credential_chain() -> None:
    calls: list[dict[str, str]] = []
    clients: list[tuple[str, object]] = []

    class FakeSession:
        def __init__(self, **kwargs: str) -> None:
            calls.append(kwargs)

        def client(self, service_name: str) -> object:
            clients.append((service_name, self))
            return object()

    create_ssm_parameter_client(region_name="us-east-1", profile_name="texas-energy", session_factory=FakeSession)
    create_ssm_parameter_client(region_name="us-east-1", session_factory=FakeSession)

    assert calls == [
        {"region_name": "us-east-1", "profile_name": "texas-energy"},
        {"region_name": "us-east-1"},
    ]
    assert [service_name for service_name, _ in clients] == ["ssm", "ssm"]
