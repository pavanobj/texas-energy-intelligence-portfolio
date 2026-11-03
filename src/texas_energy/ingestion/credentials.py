"""Secret retrieval boundaries for local ingestion and future Lambda execution."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from typing import Protocol

from .errors import IngestionError


EIA_API_KEY_PARAMETER = "/texas-energy/eia/api-key"
DEFAULT_AWS_REGION = "us-east-1"


class EiaCredentialError(IngestionError):
    """A safe failure while resolving the EIA API credential."""


class EiaCredentialAccessDenied(EiaCredentialError):
    """The active AWS identity cannot read the configured SSM parameter."""


class EiaCredentialNotFound(EiaCredentialError):
    """The configured SSM parameter does not exist."""


class EiaCredentialUnavailable(EiaCredentialError):
    """AWS credentials or an AWS region are unavailable to the SDK."""


class EiaCredentialEmpty(EiaCredentialError):
    """The SSM parameter was retrieved but did not contain a usable value."""


class SsmParameterClient(Protocol):
    """The minimal SSM client surface needed by this provider."""

    def get_parameter(self, *, Name: str, WithDecryption: bool) -> Mapping[str, object]:
        """Retrieve one parameter from AWS Systems Manager Parameter Store."""


def create_ssm_parameter_client(
    *,
    region_name: str = DEFAULT_AWS_REGION,
    profile_name: str | None = None,
    session_factory: Callable[..., object] | None = None,
) -> SsmParameterClient:
    """Create an SSM client using either an SSO profile or the default chain.

    Supplying ``profile_name`` supports local IAM Identity Center sessions. Omitting
    it leaves boto3's default credential chain intact for Lambda execution roles.
    """

    if session_factory is None:
        try:
            import boto3
            from botocore.config import Config
        except ImportError as error:
            raise EiaCredentialUnavailable("boto3 is required for SSM credential retrieval.") from error
        session_factory = boto3.Session
        config = Config(
            retries={"total_max_attempts": 2, "mode": "standard"},
            connect_timeout=5,
            read_timeout=10,
        )
    else:
        config = None

    session_arguments: dict[str, str] = {"region_name": region_name}
    if profile_name:
        session_arguments["profile_name"] = profile_name
    session = session_factory(**session_arguments)
    client_factory = getattr(session, "client")
    if config is None:
        return client_factory("ssm")
    return client_factory("ssm", config=config)


def get_eia_api_key_from_ssm(
    *,
    parameter_name: str = EIA_API_KEY_PARAMETER,
    region_name: str = DEFAULT_AWS_REGION,
    profile_name: str | None = None,
    client: SsmParameterClient | None = None,
) -> str:
    """Retrieve and validate the EIA API key without logging or persisting it."""

    ssm_client = client or create_ssm_parameter_client(
        region_name=region_name,
        profile_name=profile_name,
    )
    try:
        response = ssm_client.get_parameter(Name=parameter_name, WithDecryption=True)
    except Exception as error:
        _raise_safe_credential_error(error)
        raise AssertionError("unreachable") from error

    parameter = response.get("Parameter")
    value = parameter.get("Value") if isinstance(parameter, Mapping) else None
    if not isinstance(value, str) or not value.strip():
        raise EiaCredentialEmpty("The configured EIA SSM parameter is empty or malformed.")
    return value.strip()


def _raise_safe_credential_error(error: Exception) -> None:
    code = _aws_error_code(error)
    if code in {"AccessDenied", "AccessDeniedException", "UnauthorizedOperation"}:
        raise EiaCredentialAccessDenied("AWS access was denied for the configured EIA SSM parameter.") from error
    if code in {"ParameterNotFound", "ResourceNotFoundException"}:
        raise EiaCredentialNotFound("The configured EIA SSM parameter was not found.") from error
    if type(error).__name__ in {"NoCredentialsError", "PartialCredentialsError", "NoRegionError"}:
        raise EiaCredentialUnavailable("AWS credentials or region configuration are unavailable.") from error
    raise EiaCredentialUnavailable("Unable to retrieve the configured EIA SSM parameter.") from error


def _aws_error_code(error: Exception) -> str | None:
    response = getattr(error, "response", None)
    if not isinstance(response, Mapping):
        return None
    error_details = response.get("Error")
    if not isinstance(error_details, Mapping):
        return None
    code = error_details.get("Code")
    return code if isinstance(code, str) else None
