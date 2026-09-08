"""Process-level checks that discarded input does not accumulate in a stream."""

from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != "linux", reason="ru_maxrss is measured in KiB on Linux")

# Keep generation inside the child: neither process constructs the whole source.
_PROBE = """
import gc
import json
import resource
import sys

from json_projection import Projection

payload_bytes = int(sys.argv[1]) * 1024 * 1024
malformed = sys.argv[2] == "malformed"
projection = Projection.excluding({"drop"}) if sys.argv[3] == "excluding" else Projection({"id"})
session = projection.stream()
chunk = b"x" * (64 * 1024)
prefix = b'{"id":1,"drop":"'
gc.collect()
before = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss

session.feed(prefix)
for _ in range(payload_bytes // len(chunk)):
    session.feed(chunk)
output = None
error = None
try:
    session.feed(b'"}!' if malformed else b'"}')
    output = session.finish().decode("ascii")
except ValueError as exc:
    error = str(exc)

after = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
print(json.dumps({"delta_kib": after - before, "output": output, "error": error}))
"""


@dataclass(frozen=True)
class MemoryResult:
    delta_kib: int
    output: str | None
    error: str | None


def _measure(size_mib: int, *, malformed: bool = False, excluding: bool = False) -> MemoryResult:
    proc = subprocess.run(
        [
            sys.executable,
            "-c",
            _PROBE,
            str(size_mib),
            "malformed" if malformed else "valid",
            "excluding" if excluding else "including",
        ],
        capture_output=True,
        text=True,
        cwd=ROOT,
        timeout=120,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    return MemoryResult(**json.loads(proc.stdout))


@pytest.mark.parametrize("excluding", [False, True])
def test_discarded_string_memory_is_bounded(excluding: bool) -> None:
    smaller = _measure(8, excluding=excluding)
    larger = _measure(128, excluding=excluding)

    for result in (smaller, larger):
        assert result.error is None
        assert result.output == '{"id":1}'
        assert result.delta_kib < 32 * 1024, result
    assert larger.delta_kib <= smaller.delta_kib + 16 * 1024, (smaller, larger)


@pytest.mark.parametrize("excluding", [False, True])
def test_late_malformed_suffix_after_large_discarded_string(excluding: bool) -> None:
    result = _measure(128, malformed=True, excluding=excluding)

    assert result.output is None
    offset = len(b'{"id":1,"drop":"') + 128 * 1024 * 1024 + len(b'"}')
    assert result.error is not None
    assert f"byte {offset}:" in result.error
    assert result.delta_kib < 32 * 1024, result
