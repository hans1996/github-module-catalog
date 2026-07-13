"""Installed-wheel smoke tests independent of the source checkout."""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).parents[2]
_SENSITIVE_ENVIRONMENT_NAME = re.compile(r"(?i)(?:token|secret|password|authorization|api[_-]?key)")
_STRONG_SECRET_SHAPE = re.compile(
    r"AKIA[A-Z0-9]{16}|"
    r"sk-[A-Za-z0-9_-]{20,}|"
    r"eyJ[A-Za-z0-9_-]*\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+|"
    r"-----BEGIN [A-Z ]*PRIVATE KEY-----|"
    r"glpat-[A-Za-z0-9_-]{20,}|"
    r"xox[baprs]-[A-Za-z0-9-]{10,}|"
    r"sk_live_[A-Za-z0-9]{16,}|"
    r"gh[pousr]_[A-Za-z0-9]{20,}|"
    r"github_pat_[A-Za-z0-9_]{20,}"
)
_LABELED_SECRET = re.compile(
    r"(?i)\b(?:token|secret|password|authorization|api[_-]?key)\s*[:=]\s*\S+"
)


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
        str(virtualenv),
        cwd=tmp_path,
    )
    configuration = (virtualenv / "pyvenv.cfg").read_text(encoding="utf-8")
    assert "include-system-site-packages = false" in configuration
    executable_directory = virtualenv / ("Scripts" if os.name == "nt" else "bin")
    child_python = executable_directory / ("python.exe" if os.name == "nt" else "python")
    _run(
        "uv",
        "pip",
        "install",
        "--offline",
        "--python",
        str(child_python),
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
    try:
        subprocess.run(
            command,
            cwd=cwd,
            env=environment,
            check=True,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except subprocess.CalledProcessError as error:
        rendered_command = _redact(" ".join(command), environment)
        stdout = _redact(error.stdout or "", environment)
        stderr = _redact(error.stderr or "", environment)
        pytest.fail(
            f"command failed ({error.returncode}): {rendered_command}\n"
            f"stdout:\n{stdout}\nstderr:\n{stderr}",
            pytrace=False,
        )


def _redact(value: str, environment: dict[str, str]) -> str:
    redacted = value
    for name, secret in environment.items():
        if len(secret) >= 4 and _SENSITIVE_ENVIRONMENT_NAME.search(name):
            redacted = redacted.replace(secret, "[REDACTED]")
    redacted = _STRONG_SECRET_SHAPE.sub("[REDACTED]", redacted)
    return _LABELED_SECRET.sub("[REDACTED]", redacted)
