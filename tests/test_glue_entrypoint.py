"""Portable contract tests for the managed Glue job entrypoint."""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest


def _entrypoint_module():
    path = Path(__file__).resolve().parents[1] / "jobs" / "glue" / "run_silver_gold.py"
    spec = importlib.util.spec_from_file_location("glue_silver_gold_entrypoint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _production_entrypoint_module():
    path = Path(__file__).resolve().parents[1] / "jobs" / "glue" / "run_production_silver_gold.py"
    spec = importlib.util.spec_from_file_location("glue_production_entrypoint", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _required_application_arguments() -> list[str]:
    return [
        "--bronze-input",
        "s3://example/glue-smoke/input/v2/bronze",
        "--silver-output",
        "s3://example/glue-smoke/output/v2/silver",
        "--gold-output",
        "s3://example/glue-smoke/output/v2/gold",
    ]


def test_glue_entrypoint_accepts_observed_glue_5_platform_arguments() -> None:
    module = _entrypoint_module()

    args = module._parser().parse_args(
        _required_application_arguments()
        + [
            "--JOB_NAME",
            "texas-energy-glue-silver-gold-smoke",
            "--JOB_ID",
            "j_example",
            "--JOB_RUN_ID",
            "jr_example",
            "--glue-di-packages-correlation-ids",
            "example-correlation-id",
            "--internal-lib-urls",
            "https://example.invalid/glue-library.zip",
            "--tenant-internal",
            "glue",
            "--artifact-package-sha256",
            "a" * 64,
            "--artifact-script-sha256",
            "b" * 64,
        ]
    )

    assert args.bronze_input == "s3://example/glue-smoke/input/v2/bronze"
    assert args.gold_output == "s3://example/glue-smoke/output/v2/gold"
    assert args.JOB_RUN_ID == "jr_example"
    assert args.artifact_package_sha256 == "a" * 64
    assert args.artifact_script_sha256 == "b" * 64


def test_glue_entrypoint_rejects_unknown_application_argument() -> None:
    module = _entrypoint_module()

    with pytest.raises(SystemExit, match="2"):
        module._parser().parse_args(_required_application_arguments() + ["--silvre-output", "typo"])


def test_production_entrypoint_accepts_observed_glue_5_platform_arguments() -> None:
    module = _production_entrypoint_module()
    args = module._parser().parse_args(
        _required_application_arguments()
        + [
            "--run-id", "production-test", "--JOB_NAME", "texas-energy-glue-silver-gold-production",
            "--JOB_ID", "j_example", "--JOB_RUN_ID", "jr_example",
            "--glue-di-packages-correlation-ids", "example-correlation-id",
            "--internal-lib-urls", "https://example.invalid/glue-library.zip",
            "--tenant-internal", "glue",
        ]
    )
    assert args.run_id == "production-test"
    assert args.JOB_RUN_ID == "jr_example"


def test_production_entrypoint_rejects_unknown_application_argument() -> None:
    module = _production_entrypoint_module()
    with pytest.raises(SystemExit, match="2"):
        module._parser().parse_args(
            _required_application_arguments() + ["--run-id", "production-test", "--silvre-output", "typo"]
        )


def test_glue_entrypoint_completes_normally_for_a_zero_status(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _entrypoint_module()
    monkeypatch.setattr(module, "main", lambda: 0)

    assert module._run_glue_entrypoint() is None


def test_glue_entrypoint_preserves_nonzero_failure(monkeypatch: pytest.MonkeyPatch) -> None:
    module = _entrypoint_module()
    monkeypatch.setattr(module, "main", lambda: 1)

    with pytest.raises(RuntimeError, match="nonzero status: 1"):
        module._run_glue_entrypoint()
