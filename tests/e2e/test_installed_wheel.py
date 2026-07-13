"""Installed-wheel smoke tests independent of the source checkout."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).parents[2]


def test_installed_wheel_contains_taxonomy_and_runs_catalog_lifecycle(tmp_path: Path) -> None:
    distribution = tmp_path / "dist"
    _run(
        "uv",
        "build",
        "--wheel",
        "--no-build-isolation",
        "--out-dir",
        str(distribution),
        str(PROJECT_ROOT),
        cwd=tmp_path,
    )
    wheel = next(distribution.glob("*.whl"))
    virtualenv = tmp_path / "venv"
    _run(
        sys.executable,
        "-m",
        "venv",
        "--system-site-packages",
        str(virtualenv),
        cwd=tmp_path,
    )
    executable_directory = virtualenv / ("Scripts" if os.name == "nt" else "bin")
    _run(
        str(executable_directory / "python"),
        "-m",
        "pip",
        "install",
        "--no-deps",
        str(wheel),
        cwd=tmp_path,
    )
    ghmod = executable_directory / ("ghmod.exe" if os.name == "nt" else "ghmod")
    workspace = tmp_path / "catalog-workspace"
    for arguments in (
        ("init",),
        ("classify",),
        ("build",),
        ("validate",),
    ):
        _run(
            str(ghmod),
            *arguments,
            "--workspace",
            str(workspace),
            cwd=tmp_path,
        )


def _run(*command: str, cwd: Path) -> None:
    environment = dict(os.environ)
    environment.pop("PYTHONPATH", None)
    subprocess.run(
        command,
        cwd=cwd,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
        timeout=60,
    )
