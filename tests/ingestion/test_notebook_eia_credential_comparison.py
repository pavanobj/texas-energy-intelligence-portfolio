"""Tests for the offline credential-comparison utility without real credentials."""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


@pytest.fixture(scope="module")
def comparison_script():
    script_path = Path(__file__).resolve().parents[2] / "scripts" / "compare_notebook_eia_credential.py"
    spec = importlib.util.spec_from_file_location("notebook_eia_credential_comparison_test_module", script_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_extracts_literal_assignment_without_exposing_its_value(
    comparison_script, tmp_path: Path
) -> None:
    notebook = tmp_path / "reference.ipynb"
    notebook.write_text(
        '{"cells":[{"cell_type":"code","source":["API_KEY = \'test-only-key\'\\n"]}]}',
        encoding="utf-8",
    )

    assert comparison_script._notebook_eia_key(notebook) == "test-only-key"


def test_rejects_notebooks_without_a_literal_key(comparison_script, tmp_path: Path) -> None:
    notebook = tmp_path / "reference.ipynb"
    notebook.write_text('{"cells":[{"cell_type":"code","source":["API_KEY = os.getenv(\'EIA_API_KEY\')"]}]}', encoding="utf-8")

    with pytest.raises(ValueError, match="no literal EIA API key"):
        comparison_script._notebook_eia_key(notebook)
