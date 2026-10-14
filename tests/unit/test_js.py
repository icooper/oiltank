"""The web UI's JavaScript tests (``tests/js``), run with Node's built-in test runner.

They need the Node version in ``.nvmrc`` on the PATH (``nvm use``); CI installs it with
actions/setup-node. Without Node they're skipped locally and fail in CI. Node is never part of the
image.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

from oiltank.web.app import STATIC

REPO = Path(__file__).resolve().parents[2]
TESTS = REPO / "tests" / "js"


@pytest.fixture(scope="module")
def node() -> str:
    """The ``node`` on the PATH, checked against ``.nvmrc``."""
    wanted = (REPO / ".nvmrc").read_text().strip().removeprefix("v")
    found = shutil.which("node")
    if found is None:
        if os.environ.get("CI"):
            pytest.fail(f"Node {wanted} isn't installed (see .nvmrc)")
        pytest.skip(f"needs Node {wanted} on the PATH (see .nvmrc; `nvm use`)")
    version = subprocess.run(
        [found, "--version"], capture_output=True, text=True, check=True
    ).stdout.strip()
    have = version.removeprefix("v")
    if have != wanted and not have.startswith(f"{wanted}."):
        pytest.fail(f"the web UI's tests need Node {wanted} (.nvmrc), not {version}; `nvm use`")
    return found


def test_javascript(node: str) -> None:
    files = sorted(str(f) for f in TESTS.glob("*.test.js"))
    assert files
    result = subprocess.run(
        [node, "--test", *files],
        capture_output=True,
        text=True,
        timeout=120,
        env={**os.environ, "TZ": "UTC"},  # tick labels are local times
    )
    assert result.returncode == 0, result.stdout + result.stderr


def test_every_module_parses(node: str) -> None:
    """Including the ones that need a browser to run, which the tests above can't import.

    The source goes in on stdin: ``node --check file.js`` doesn't parse a file as a module unless
    a package.json says so, and passes a broken one.
    """
    for f in sorted(STATIC.rglob("*.js")):
        result = subprocess.run(
            [node, "--input-type=module", "--check"],
            input=f.read_text(),
            capture_output=True,
            text=True,
            timeout=60,
        )
        assert result.returncode == 0, f"{f.name}: {result.stderr}"
