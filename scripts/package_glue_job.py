"""Create a deterministic source bundle for the Glue 5 Silver/Gold smoke job.

The bundle intentionally contains only the project package.  Glue 5 supplies
PySpark and Delta; this command neither vendors them nor contacts AWS.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import zipfile


_ZIP_TIMESTAMP = (2024, 1, 1, 0, 0, 0)


def build_package(*, source_root: Path, output: Path) -> tuple[str, ...]:
    package_root = source_root / "texas_energy"
    publisher_candidate = source_root.parent / "scripts" / "publish_static_dashboard_data.py"
    publisher_source = publisher_candidate if publisher_candidate.is_file() else None
    if not package_root.is_dir():
        raise ValueError(f"Missing package root: {package_root}")

    members = tuple(
        sorted(
            path
            for path in package_root.rglob("*.py")
            if "__pycache__" not in path.parts
        )
    )
    if not members:
        raise ValueError("No Python package files found for Glue bundle.")

    output.parent.mkdir(parents=True, exist_ok=True)
    # Store rather than deflate source members.  Compression output can vary by
    # zlib build, while ZIP_STORED keeps the reviewed SHA-256 identical on
    # Windows and GitHub's Linux runners.  Glue downloads this small source
    # bundle once per run, so reproducibility is more valuable than compression.
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_STORED) as archive:
        if publisher_source is not None:
            publisher_info = zipfile.ZipInfo(publisher_source.name, date_time=_ZIP_TIMESTAMP)
            publisher_info.compress_type = zipfile.ZIP_STORED
            publisher_info.create_system = 3
            publisher_info.external_attr = 0o100644 << 16
            publisher_contents = publisher_source.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
            archive.writestr(publisher_info, publisher_contents)
        for path in members:
            info = zipfile.ZipInfo(path.relative_to(source_root).as_posix(), date_time=_ZIP_TIMESTAMP)
            info.compress_type = zipfile.ZIP_STORED
            # ZipInfo otherwise embeds the host platform (Windows=0, Unix=3)
            # in the central directory, changing bytes despite identical files.
            info.create_system = 3
            info.external_attr = 0o100644 << 16
            # Source files are text.  Normalize their line endings so the
            # reviewed archive SHA-256 is stable between Windows/WSL and a
            # GitHub Linux runner, regardless of the checkout EOL setting.
            contents = path.read_text(encoding="utf-8").replace("\r\n", "\n").encode("utf-8")
            archive.writestr(info, contents)
    package_members = [path.relative_to(source_root).as_posix() for path in members]
    if publisher_source is not None:
        package_members.append(publisher_source.name)
    return tuple(package_members)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="Local ZIP output path; upload is intentionally separate.")
    parser.add_argument("--source-root", type=Path, default=Path(__file__).resolve().parents[1] / "src")
    args = parser.parse_args()
    members = build_package(source_root=args.source_root, output=args.output)
    print(f"{{\"files\": {len(members)}, \"output\": \"{args.output}\"}}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
