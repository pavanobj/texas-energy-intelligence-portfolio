"""Focused offline contracts for the non-deployed AgentCore adapter."""
from __future__ import annotations

import hashlib
import io
import json
from pathlib import Path

import pytest

from texas_energy.cloud.agentcore import CloudAgentConfigurationError, CloudAgentSettings, CloudArtifactError, DashboardDataMaterializer, S3ArtifactMaterializer, create_agentcore_app


class _S3:
    def __init__(self, objects: dict[str, bytes]) -> None:
        self.objects = objects
        self.calls: list[tuple[str, str]] = []

    def get_object(self, *, Bucket: str, Key: str) -> dict[str, object]:
        self.calls.append((Bucket, Key))
        if Key not in self.objects:
            raise RuntimeError("missing")
        return {"Body": io.BytesIO(self.objects[Key])}


def _entry(key: str, destination: str, data: bytes) -> dict[str, object]:
    return {"key": key, "destination": destination, "sha256": hashlib.sha256(data).hexdigest(), "size_bytes": len(data)}


def test_cloud_materializer_only_accepts_manifest_allowlist(tmp_path: Path) -> None:
    files = {
        "private-agent/v1/closeout/historical_demo/heldout_2025-07-01_06z_24h.parquet": b"parquet",
        "private-agent/v1/closeout/comparison/leaderboard.json": b"[]",
        "private-agent/v1/closeout/comparison/per_horizon_leaderboard.json": b"[]",
        "private-agent/v1/closeout/model_catalog.json": b"{}",
        "private-agent/v1/rag/chunks.jsonl": b"{}\n",
        "private-agent/v1/rag/embeddings.npy": b"numpy",
        "private-agent/v1/rag/manifest.json": b"{}",
        "private-agent/v1/rag/embedding-model/config.json": b"{}",
    }
    entries = [_entry(key, key.removeprefix("private-agent/v1/"), value) for key, value in files.items()]
    manifest_key = "private-agent/v1/manifest.json"
    files[manifest_key] = json.dumps({"artifacts": entries}).encode()
    settings = CloudAgentSettings(artifact_bucket="private-agent-artifacts", data_lake_bucket="canonical-data-lake", manifest_key=manifest_key, model_id="amazon.nova-micro-v1:0", cache_root=tmp_path)
    fake = _S3(files)
    closeout, rag, model = S3ArtifactMaterializer(client=fake, settings=settings).materialize()
    assert (closeout / "model_catalog.json").read_bytes() == b"{}"
    assert (rag / "embeddings.npy").read_bytes() == b"numpy"
    assert (model / "config.json").read_bytes() == b"{}"
    assert {key for _, key in fake.calls} == set(files)


def test_cloud_settings_reject_missing_or_unsafe_configuration() -> None:
    with pytest.raises(CloudAgentConfigurationError):
        CloudAgentSettings.from_environment({})
    with pytest.raises(CloudAgentConfigurationError):
        CloudAgentSettings(artifact_bucket="Invalid_Bucket", data_lake_bucket="canonical-data-lake", manifest_key="private-agent/v1/manifest.json", model_id="model")
    with pytest.raises(CloudAgentConfigurationError):
        CloudAgentSettings(artifact_bucket="private-agent-artifacts", data_lake_bucket="canonical-data-lake", manifest_key="../manifest.json", model_id="model")


def test_cloud_materializer_rejects_extra_non_model_objects(tmp_path: Path) -> None:
    manifest_key = "private-agent/v1/manifest.json"
    settings = CloudAgentSettings(artifact_bucket="private-agent-artifacts", data_lake_bucket="canonical-data-lake", manifest_key=manifest_key, model_id="amazon.nova-micro-v1:0", cache_root=tmp_path)
    fake = _S3({manifest_key: json.dumps({"artifacts": [_entry("private-agent/v1/unapproved.txt", "unapproved.txt", b"no")] }).encode()})
    with pytest.raises(CloudArtifactError):
        S3ArtifactMaterializer(client=fake, settings=settings).materialize()


def test_http_adapter_has_health_and_bounded_invocation_contract() -> None:
    fastapi = pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    class Service:
        def answer(self, question: str) -> dict[str, object]:
            assert question == "Which model performed best?"
            return {"answer": "Historical evidence only.", "tool_observations": [{"name": "get_model_performance"}]}

    client = TestClient(create_agentcore_app(Service()))
    assert client.get("/ping").json() == {"status": "Healthy"}
    assert client.post("/invocations", json={"question": "Which model performed best?"}).status_code == 200
    assert client.post("/invocations", json={"question": "x", "unexpected": "path"}).status_code == 422
