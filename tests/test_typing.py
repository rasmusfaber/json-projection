import importlib.util
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _run(*cmd: str) -> None:
    proc = subprocess.run([sys.executable, "-m", *cmd], capture_output=True, text=True, cwd=ROOT)
    assert proc.returncode == 0, proc.stdout + proc.stderr


@pytest.mark.skipif(importlib.util.find_spec("mypy") is None, reason="mypy not installed")
def test_mypy_strict_on_package():
    _run("mypy", "--strict", "python/json_projection")


@pytest.mark.skipif(importlib.util.find_spec("basedpyright") is None, reason="basedpyright not installed")
def test_basedpyright_on_package_and_tests():
    _run("basedpyright", "python", "tests")
