"""The uv version is set once, as [tool.uv] required-version in pyproject.toml.

The workflows' setup-uv steps read it from there. The Dockerfile can't read it, so its default
has to match.
"""

import re
import tomllib
from pathlib import Path
from typing import Any

import yaml

REPO = Path(__file__).resolve().parents[2]


def _required_version() -> str:
    pyproject = tomllib.loads((REPO / "pyproject.toml").read_text())
    pin = pyproject["tool"]["uv"]["required-version"]
    match = re.fullmatch(r"==(\d+\.\d+\.\d+)", pin)
    assert match, f"[tool.uv] required-version should be an exact pin (==X.Y.Z), not {pin!r}"
    return match[1]


def test_dockerfile_matches_pyproject() -> None:
    dockerfile = (REPO / "Dockerfile").read_text()
    assert re.findall(r"^ARG UV_VERSION=(\S+)$", dockerfile, re.M) == [_required_version()]


def test_workflows_read_the_version_from_pyproject() -> None:
    """setup-uv only falls back to pyproject.toml when a step doesn't name its own version."""
    own_version = []
    for path in sorted((REPO / ".github" / "workflows").glob("*.yml")):
        workflow: dict[str, Any] = yaml.safe_load(path.read_text())
        for job_id, job in workflow["jobs"].items():
            for step in job.get("steps", []):
                uses_setup_uv = str(step.get("uses", "")).startswith("astral-sh/setup-uv@")
                if uses_setup_uv and {"version", "version-file"} & step.get("with", {}).keys():
                    own_version.append(f"{path.name} ({job_id})")
    assert not own_version, f"setup-uv steps that set their own version: {own_version}"
