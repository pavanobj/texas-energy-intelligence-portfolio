"""Focused contract checks for public portfolio architecture diagrams."""
from __future__ import annotations

import importlib.util
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
GENERATOR = ROOT / "scripts" / "generate_portfolio_diagrams.py"


def _load_generator():
    spec = importlib.util.spec_from_file_location("portfolio_diagrams", GENERATOR)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_generator_declares_balanced_layers_and_real_services() -> None:
    module = _load_generator()
    for diagram in (module.PRODUCTION_DOT, module.MLOPS_DOT):
        assert 'size="16,9!"' in diagram
        assert 'ratio="fill"' in diagram
        assert "fillcolor" in diagram

    for service in (
        "AWS Lambda",
        "Amazon S3",
        "AWS Glue + Spark",
        "AWS Step Functions",
        "SageMaker Processing",
        "XGBoost",
        "Bi-LSTM",
        "Chronos-2",
        "Amazon CloudFront",
        "Amazon Bedrock AgentCore",
    ):
        assert service in module.PRODUCTION_DOT

    for capability in (
        "GitHub Actions",
        "Terraform",
        "Amazon ECR",
        "SageMaker training",
        "Human approval gate",
        "No automatic promotion",
        "Prospective evaluation",
    ):
        assert capability in module.MLOPS_DOT


def test_exported_diagrams_are_editable_and_rendered() -> None:
    architecture = ROOT / "docs" / "architecture"
    for stem in ("production-architecture", "mlops-cicd"):
        drawio = (architecture / f"{stem}.drawio").read_text(encoding="utf-8")
        svg = (architecture / f"{stem}.svg").read_text(encoding="utf-8")
        png = architecture / f"{stem}.png"
        assert "swimlane" in drawio
        assert "<svg" in svg
        assert png.stat().st_size > 100_000
        dimensions = re.search(r'<svg width="([0-9.]+)pt" height="([0-9.]+)pt"', svg)
        viewbox = re.search(r'viewBox="0.00 0.00 ([0-9.]+) ([0-9.]+)"', svg)
        assert dimensions and viewbox
        assert dimensions.groups() == viewbox.groups()
