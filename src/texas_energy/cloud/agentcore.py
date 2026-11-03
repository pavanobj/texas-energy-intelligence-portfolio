"""Private AgentCore HTTP adapter with immutable, read-only S3 artifacts.

This module does not create AWS resources. At runtime it uses the execution
role supplied by AgentCore through boto3's default credential chain; it never
accepts caller-supplied bucket names, object keys, or filesystem paths.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Protocol

from texas_energy.analytics.agent import AgentLimits, EnergyAgentError, EnergyIntelligenceAgent, create_bedrock_agent
from texas_energy.analytics.dashboard_tools import DashboardDataStore, DashboardToolError
from texas_energy.analytics.tools import AnalyticsArtifactStore


_BUCKET_RE = re.compile(r"^(?!xn--)(?!.*\.\.)(?!.*\.-)(?!.*-\.)(?:[a-z0-9](?:[a-z0-9.-]{1,61}[a-z0-9])?)$")
_SAFE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,510}$")
_MAX_MANIFEST_BYTES = 128 * 1024
# The clean MiniLM serving bundle includes model weights larger than the small
# analytics files. This remains a hard cap, rather than an unbounded download.
_MAX_OBJECT_BYTES = 256 * 1024 * 1024
_MAX_DASHBOARD_DOCUMENT_BYTES = 2 * 1024 * 1024
_DASHBOARD_OBJECTS = {
    "prospective-forecast.json": "dashboard/v1/data/prospective-forecast.json",
    "prospective-evaluation-manifest.json": "dashboard/v1/data/prospective-evaluation-manifest.json",
    "rolling-prospective-evaluation.json": "dashboard/v1/data/rolling-prospective-evaluation.json",
    "rolling-historical-replay.json": "dashboard/v1/data/rolling-historical-replay.json",
}


class CloudAgentConfigurationError(ValueError):
    """Cloud agent configuration is unsafe or incomplete."""


class CloudArtifactError(RuntimeError):
    """Trusted private cloud evidence could not be materialized safely."""


class S3Client(Protocol):
    def get_object(self, *, Bucket: str, Key: str) -> Mapping[str, Any]: ...


@dataclass(frozen=True)
class CloudAgentSettings:
    """Non-secret deployment configuration sourced from AgentCore variables."""

    artifact_bucket: str
    data_lake_bucket: str
    manifest_key: str
    model_id: str
    region_name: str = "us-east-1"
    cache_root: Path = Path("/tmp/texas-energy-agent")
    max_steps: int = 4
    max_tool_calls: int = 6
    timeout_seconds: float = 45.0
    max_output_tokens: int = 256

    @classmethod
    def from_environment(cls, environ: Mapping[str, str] | None = None) -> "CloudAgentSettings":
        values = os.environ if environ is None else environ
        required = {
            "artifact_bucket": values.get("TEXAS_ENERGY_AGENT_ARTIFACT_BUCKET", ""),
            "data_lake_bucket": values.get("TEXAS_ENERGY_AGENT_DATA_LAKE_BUCKET", ""),
            "manifest_key": values.get("TEXAS_ENERGY_AGENT_ARTIFACT_MANIFEST_KEY", ""),
            "model_id": values.get("TEXAS_ENERGY_AGENT_MODEL_ID", ""),
        }
        if any(not value for value in required.values()):
            raise CloudAgentConfigurationError("Required private agent configuration is unavailable.")
        return cls(
            **required,
            region_name=values.get("AWS_REGION", values.get("TEXAS_ENERGY_AGENT_REGION", "us-east-1")),
            cache_root=Path(values.get("TEXAS_ENERGY_AGENT_CACHE_ROOT", "/tmp/texas-energy-agent")),
        )

    def __post_init__(self) -> None:
        if not _BUCKET_RE.fullmatch(self.artifact_bucket):
            raise CloudAgentConfigurationError("Artifact bucket configuration is invalid.")
        if not _BUCKET_RE.fullmatch(self.data_lake_bucket):
            raise CloudAgentConfigurationError("Data lake bucket configuration is invalid.")
        if not _safe_key(self.manifest_key):
            raise CloudAgentConfigurationError("Artifact manifest configuration is invalid.")
        if not self.model_id or len(self.model_id) > 256:
            raise CloudAgentConfigurationError("Bedrock model configuration is invalid.")
        if self.max_steps < 1 or self.max_tool_calls < 1 or self.timeout_seconds <= 0 or self.max_output_tokens < 1:
            raise CloudAgentConfigurationError("Agent execution bounds must be positive.")

    def limits(self) -> AgentLimits:
        return AgentLimits(max_steps=self.max_steps, max_tool_calls=self.max_tool_calls, timeout_seconds=self.timeout_seconds, max_output_tokens=self.max_output_tokens)


def _safe_key(value: object) -> bool:
    return isinstance(value, str) and bool(_SAFE_KEY_RE.fullmatch(value)) and not value.startswith("/") and ".." not in value.split("/")


def _read_limited(body: Any, *, limit: int) -> bytes:
    try:
        data = body.read(limit + 1)
    finally:
        close = getattr(body, "close", None)
        if callable(close):
            close()
    if not isinstance(data, bytes) or len(data) > limit:
        raise CloudArtifactError("A trusted artifact exceeded the configured safe size limit.")
    return data


@dataclass(frozen=True)
class ArtifactEntry:
    destination: str
    key: str
    sha256: str
    size_bytes: int

    @classmethod
    def parse(cls, value: object) -> "ArtifactEntry":
        if not isinstance(value, Mapping):
            raise CloudArtifactError("The trusted artifact manifest is malformed.")
        destination, key, digest, size = value.get("destination"), value.get("key"), value.get("sha256"), value.get("size_bytes")
        if not all(isinstance(item, str) for item in (destination, key, digest)) or not isinstance(size, int):
            raise CloudArtifactError("The trusted artifact manifest is malformed.")
        if not _safe_key(destination) or not _safe_key(key) or len(digest) != 64 or any(character not in "0123456789abcdef" for character in digest.lower()):
            raise CloudArtifactError("The trusted artifact manifest contains unsafe locations or checksums.")
        if not 0 < size <= _MAX_OBJECT_BYTES:
            raise CloudArtifactError("The trusted artifact manifest contains an unsafe object size.")
        return cls(destination=destination, key=key, sha256=digest.lower(), size_bytes=size)


class S3ArtifactMaterializer:
    """Copy a manifest allowlist into an ephemeral cache and verify every byte."""

    def __init__(self, *, client: S3Client, settings: CloudAgentSettings) -> None:
        self._client, self._settings = client, settings

    def _get(self, key: str, *, limit: int) -> bytes:
        try:
            response = self._client.get_object(Bucket=self._settings.artifact_bucket, Key=key)
            return _read_limited(response["Body"], limit=limit)
        except CloudArtifactError:
            raise
        except Exception:
            raise CloudArtifactError("A required private agent artifact could not be read.") from None

    def materialize(self) -> tuple[Path, Path, Path]:
        raw_manifest = self._get(self._settings.manifest_key, limit=_MAX_MANIFEST_BYTES)
        try:
            manifest = json.loads(raw_manifest)
            entries = [ArtifactEntry.parse(item) for item in manifest["artifacts"]]
        except (KeyError, TypeError, json.JSONDecodeError, CloudArtifactError):
            raise CloudArtifactError("The trusted artifact manifest is invalid.") from None
        expected = {"closeout/historical_demo/heldout_2025-07-01_06z_24h.parquet", "closeout/comparison/leaderboard.json", "closeout/comparison/per_horizon_leaderboard.json", "closeout/model_catalog.json", "rag/chunks.jsonl", "rag/embeddings.npy", "rag/manifest.json", "rag/embedding-model/config.json"}
        destinations = {entry.destination for entry in entries}
        if not expected.issubset(destinations) or len(destinations) != len(entries):
            raise CloudArtifactError("The artifact manifest does not contain the approved private-agent evidence set.")
        if any(destination not in expected and not destination.startswith("rag/embedding-model/") for destination in destinations):
            raise CloudArtifactError("The artifact manifest includes an unapproved private-agent object.")
        cache_id = hashlib.sha256(raw_manifest).hexdigest()[:20]
        root = self._settings.cache_root / cache_id
        root.mkdir(parents=True, exist_ok=True)
        for entry in entries:
            target = root / entry.destination
            target.parent.mkdir(parents=True, exist_ok=True)
            if target.is_file() and target.stat().st_size == entry.size_bytes and hashlib.sha256(target.read_bytes()).hexdigest() == entry.sha256:
                continue
            data = self._get(entry.key, limit=entry.size_bytes)
            if len(data) != entry.size_bytes or hashlib.sha256(data).hexdigest() != entry.sha256:
                raise CloudArtifactError("A private agent artifact failed integrity verification.")
            descriptor, temporary_name = tempfile.mkstemp(prefix="artifact-", dir=target.parent)
            os.close(descriptor)
            temporary = Path(temporary_name)
            try:
                temporary.write_bytes(data)
                temporary.replace(target)
            finally:
                temporary.unlink(missing_ok=True)
        return root / "closeout", root / "rag", root / "rag/embedding-model"


class DashboardDataMaterializer:
    """Load four fixed, published dashboard objects with integrity evidence.

    The AgentCore container receives no object key, prefix, or bucket input from
    the caller. The execution role is limited to these exact SSE-S3 objects.
    """

    def __init__(self, *, client: S3Client, settings: CloudAgentSettings) -> None:
        self._client, self._settings = client, settings

    def materialize(self) -> DashboardDataStore:
        documents: dict[str, Mapping[str, Any]] = {}
        checksums: dict[str, str] = {}
        for name, key in _DASHBOARD_OBJECTS.items():
            try:
                response = self._client.get_object(Bucket=self._settings.data_lake_bucket, Key=key)
                raw = _read_limited(response["Body"], limit=_MAX_DASHBOARD_DOCUMENT_BYTES)
                document = json.loads(raw)
            except (KeyError, TypeError, json.JSONDecodeError, CloudArtifactError):
                raise CloudArtifactError("A required published dashboard document could not be read safely.") from None
            except Exception:
                raise CloudArtifactError("A required published dashboard document could not be read safely.") from None
            if not isinstance(document, Mapping):
                raise CloudArtifactError("A published dashboard document has an invalid schema.")
            documents[name] = document
            checksums[name] = hashlib.sha256(raw).hexdigest()
        try:
            return DashboardDataStore(documents=documents, sha256=checksums)
        except DashboardToolError:
            raise CloudArtifactError("Published dashboard evidence did not meet the trusted tool contract.") from None


@dataclass(frozen=True)
class CloudAgentService:
    agent: EnergyIntelligenceAgent

    @classmethod
    def from_settings(cls, settings: CloudAgentSettings) -> "CloudAgentService":
        import boto3
        from botocore.config import Config

        session = boto3.Session(region_name=settings.region_name)
        s3 = session.client("s3", config=Config(retries={"total_max_attempts": 2, "mode": "adaptive"}, connect_timeout=3, read_timeout=10, max_pool_connections=1))
        closeout_root, rag_root, embedding_model_root = S3ArtifactMaterializer(client=s3, settings=settings).materialize()
        dashboard_store = DashboardDataMaterializer(client=s3, settings=settings).materialize()
        return cls(agent=create_bedrock_agent(model_id=settings.model_id, store=AnalyticsArtifactStore(closeout_root), dashboard_store=dashboard_store, rag_index_root=rag_root, rag_embedding_model_path=embedding_model_root, region_name=settings.region_name, limits=settings.limits()))

    def answer(self, question: str) -> dict[str, Any]:
        return self.agent.answer(question).as_dict()


def create_agentcore_app(service: CloudAgentService | None = None, *, settings: CloudAgentSettings | None = None) -> Any:
    """Create the AgentCore HTTP protocol app; AWS auth is enforced at the endpoint."""
    try:
        from fastapi import Body, FastAPI, HTTPException
    except ImportError as exc:  # pragma: no cover - deployment dependency
        raise RuntimeError("Install the cloud-agent dependency group to run the AgentCore adapter.") from exc

    active_service = service
    app = FastAPI(title="Texas Energy Intelligence Private Agent", docs_url=None, redoc_url=None, openapi_url=None)

    @app.get("/ping")
    def ping() -> dict[str, str]:
        return {"status": "Healthy"}

    @app.post("/invocations")
    def invoke(request: dict[str, Any] = Body(...)) -> dict[str, Any]:
        nonlocal active_service
        try:
            if set(request) != {"question"} or not isinstance(request["question"], str) or not request["question"].strip() or len(request["question"]) > 200:
                raise HTTPException(status_code=422, detail="A single non-empty question of at most 200 characters is required.")
            if active_service is None:
                active_service = CloudAgentService.from_settings(settings or CloudAgentSettings.from_environment())
            return active_service.answer(request["question"].strip())
        except HTTPException:
            raise
        except (CloudAgentConfigurationError, CloudArtifactError, EnergyAgentError):
            raise HTTPException(status_code=503, detail="Private agent evidence is temporarily unavailable.") from None
        except Exception:
            raise HTTPException(status_code=500, detail="Private agent invocation could not complete safely.") from None

    return app
