from __future__ import annotations

import importlib.util
from pathlib import Path
import zipfile


def _module():
    path = Path(__file__).resolve().parents[1] / "scripts" / "package_glue_job.py"
    spec = importlib.util.spec_from_file_location("package_glue_job", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_glue_package_contains_project_sources_and_the_proven_dashboard_reader(tmp_path: Path) -> None:
    module = _module()
    output = tmp_path / "texas_energy_glue_job.zip"

    members = module.build_package(source_root=Path(__file__).resolve().parents[1] / "src", output=output)

    assert output.is_file()
    assert "texas_energy/spark/silver.py" in members
    assert "texas_energy/spark/gold.py" in members
    with zipfile.ZipFile(output) as archive:
        names = archive.namelist()
    assert names == sorted(names)
    assert "publish_static_dashboard_data.py" in names
    assert all(
        (name.startswith("texas_energy/") or name == "publish_static_dashboard_data.py") and name.endswith(".py")
        for name in names
    )


def test_glue_package_normalizes_python_line_endings(tmp_path: Path) -> None:
    module = _module()
    source_root = tmp_path / "src"
    package = source_root / "texas_energy"
    package.mkdir(parents=True)
    (package / "example.py").write_bytes(b"value = 1\r\n")

    output = tmp_path / "normalized.zip"
    module.build_package(source_root=source_root, output=output)

    with zipfile.ZipFile(output) as archive:
        assert archive.read("texas_energy/example.py") == b"value = 1\n"
